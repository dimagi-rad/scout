"""Real managed-PostgreSQL publication/expiry with artifact recovery endpoints.

The Cube HTTP boundary is replaced only for successful count queries, executing
the count against the actual view under its readonly role. Catalog, schema DDL,
teardown/rebuild tasks, semantic compiler, authorization, and endpoint are real.
No provider is called and every physical schema is owned by this fixture.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.db import connections
from django.test import AsyncClient
from psycopg import sql

from apps.artifacts.models import Artifact, ArtifactType
from apps.common.identifiers import view_name
from apps.semantic.models import SemanticDataset
from apps.semantic.services.catalog import SemanticCatalogUnavailable, ensure_semantic_model
from apps.semantic.services.cube_schema import build_and_promote_cube_schema
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.tasks import rebuild_workspace_view_schema, teardown_schema
from mcp_server.context import load_workspace_context
from mcp_server.services.pool import close_all_pools
from mcp_server.services.query import _execute_async_parameterized

pytestmark = pytest.mark.django_db(transaction=True)


def _create_table(dsn, schema_name, table_name="raw_visits", count=2):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE TABLE {}.{} (id integer PRIMARY KEY, category text)").format(
                sql.Identifier(schema_name), sql.Identifier(table_name)
            )
        )
        for number in range(count):
            conn.execute(
                sql.SQL("INSERT INTO {}.{} VALUES (%s, %s)").format(
                    sql.Identifier(schema_name), sql.Identifier(table_name)
                ),
                (number, "Synthetic"),
            )


def _login(client, user):
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)


@pytest_asyncio.fixture
async def published_sources(settings, monkeypatch):
    if not settings.MANAGED_DATABASE_URL:
        pytest.skip("MANAGED_DATABASE_URL not set")

    # A schema compiler/HTTP regression must not accidentally reach a provider.
    async def no_http(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in synthetic provenance tests")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_http)
    settings.CUBE_API_URL = ""
    settings.CUBE_VALIDATOR_URL = ""
    settings.CUBEJS_API_SECRET = ""
    settings.CUBE_SCHEMA_VALIDATION_REQUIRED = False
    owned_schemas = []
    view = None
    manager = SchemaManager()
    workspace = await Workspace.objects.acreate(name="Synthetic source provenance")

    def create_sources():
        user = User.objects.create_user(
            email=f"provenance-{uuid4().hex}@example.com", password="test"
        )
        WorkspaceMembership.objects.create(
            workspace=workspace, user=user, role=WorkspaceRole.MANAGE
        )
        tenants = [
            Tenant.objects.create(
                provider="commcare", external_id=f"provenance-{uuid4().hex}", canonical_name=name
            )
            for name in ("日" * 22, "A" * 32)
        ]
        for tenant in tenants:
            WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
            TenantMembership.objects.bulk_create([TenantMembership(user=user, tenant=tenant)])
            schema = manager.provision(tenant)
            owned_schemas.append(schema)
            _create_table(settings.MANAGED_DATABASE_URL, schema.schema_name)
        _create_table(settings.MANAGED_DATABASE_URL, owned_schemas[1].schema_name, "x" * 63)
        return user, tenants

    try:
        user, tenants = await sync_to_async(create_sources)()
        view = await sync_to_async(manager.build_view_schema)(workspace)
        cube = await sync_to_async(build_and_promote_cube_schema)(workspace)
        dataset = await SemanticDataset.objects.aget(
            workspace=workspace,
            table_name=view_name(manager._view_prefix(tenants[0]), "raw_visits"),
        )
        artifact = await Artifact.objects.acreate(
            workspace=workspace,
            created_by=user,
            title="Synthetic Unicode visits",
            artifact_type=ArtifactType.STORY,
            code="",
            semantic_queries=[{"name": "visits", "measures": [f"{dataset.name}.count"]}],
        )
        client = AsyncClient()
        await sync_to_async(_login)(client, user)
        yield SimpleNamespace(
            workspace=workspace,
            user=user,
            tenants=tenants,
            schemas=owned_schemas,
            view=view,
            cube=cube,
            dataset=dataset,
            artifact=artifact,
            client=client,
            url=f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/query-data/",
            dsn=settings.MANAGED_DATABASE_URL,
        )
    finally:
        cleanup_errors = []

        async def attempt_cleanup(stage, operation):
            try:
                return await operation()
            except BaseException as error:
                # Retain cancellation/interrupts too, but still attempt every
                # owned cleanup and closing the fixture's connections first.
                error.add_note(f"Synthetic provenance fixture cleanup: {stage}")
                cleanup_errors.append(error)

        try:
            await attempt_cleanup("query pools", close_all_pools)
            current_view = await attempt_cleanup(
                "owned view lookup",
                lambda: WorkspaceViewSchema.objects.filter(workspace=workspace).afirst(),
            )
            # A failed lookup must not discard the exact owned view returned
            # during setup. Do not infer or clean any other workspace's schema.
            if current_view is not None:
                view = current_view
            if view is not None:
                await attempt_cleanup(
                    "owned view teardown", lambda: sync_to_async(manager.teardown_view_schema)(view)
                )
            for index, schema in enumerate(owned_schemas):
                await attempt_cleanup(
                    f"owned source schema {index}",
                    lambda schema=schema: sync_to_async(manager.teardown)(schema),
                )
        finally:
            await attempt_cleanup(
                "Django connections", lambda: sync_to_async(connections.close_all)()
            )
        if cleanup_errors:
            raise BaseExceptionGroup("Synthetic provenance fixture cleanup failed", cleanup_errors)


async def _assert_recovery(setup, action="materialization"):
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as query:
        response = await setup.client.get(setup.url)
    assert response.status_code == 409
    state = response.json()["data_recovery"]
    assert state["queryable"] is False
    assert state["status"] == f"needs_{action}"
    assert state["recovery_action"] == action
    query.assert_not_awaited()


async def _assert_real_count(setup, count):
    async def execute(cube_query, *, security_context):
        assert cube_query["measures"] == [f"{setup.dataset.name}.count"]
        assert security_context["workspaceId"] == str(setup.workspace.id)
        assert security_context["userId"] == str(setup.user.id)
        ctx = await load_workspace_context(str(setup.workspace.id))
        assert security_context["readonlyRole"] == ctx.readonly_role
        statement = (
            sql.SQL("SELECT count(*) FROM {}.{}")
            .format(sql.Identifier(ctx.schema_name), sql.Identifier(setup.dataset.table_name))
            .as_string()
        )
        return await _execute_async_parameterized(ctx, statement, (), 5)

    with patch(
        "apps.semantic.services.query.CubeClient.execute_query", new=AsyncMock(side_effect=execute)
    ) as cube:
        response = await setup.client.get(setup.url)
    assert response.status_code == 200
    assert response.json()["queries"][0]["rows"] == [[count]]
    cube.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_catalog", [False, True])
async def test_expiry_partial_rebuild_and_correct_source_restore(published_sources, failed_catalog):
    setup = published_sources
    expected_source = {"tenant_id": str(setup.tenants[0].id), "source_table_name": "raw_visits"}
    assert setup.view.view_sources["views"][setup.dataset.table_name] == expected_source
    assert len(setup.dataset.table_name.encode()) == 63
    assert setup.dataset.metadata["source_tenant_ids"] == [str(setup.tenants[0].id)]
    await _assert_real_count(setup, 2)

    await TenantSchema.objects.filter(pk=setup.schemas[0].pk).aupdate(state=SchemaState.TEARDOWN)
    await _assert_recovery(setup)
    await teardown_schema.func(str(setup.schemas[0].id))
    await setup.view.arefresh_from_db()
    assert setup.view.state == SchemaState.FAILED
    assert setup.view.view_sources["views"][setup.dataset.table_name] == expected_source
    await _assert_recovery(setup)

    if failed_catalog:
        with patch(
            "apps.semantic.services.cube_schema.ensure_semantic_model",
            side_effect=RuntimeError("Synthetic catalog failure"),
        ):
            result = await rebuild_workspace_view_schema.func(str(setup.workspace.id))
        assert result["cube_schema"]["ok"] is False
    else:
        result = await rebuild_workspace_view_schema.func(str(setup.workspace.id))
        assert result["cube_schema"]["ok"] is True
    await setup.view.arefresh_from_db()
    assert setup.view.state == SchemaState.ACTIVE
    assert setup.dataset.table_name not in setup.view.view_sources["views"]
    await setup.dataset.arefresh_from_db()
    assert setup.dataset.is_visible is failed_catalog
    assert setup.dataset.metadata["source_tenant_ids"] == [str(setup.tenants[0].id)]
    await _assert_recovery(setup)

    # Restore only A's exact source record/name; B's source remains unchanged.
    restored = await sync_to_async(SchemaManager().provision)(setup.tenants[0])
    assert restored.id == setup.schemas[0].id
    await sync_to_async(_create_table)(setup.dsn, restored.schema_name, count=3)
    result = await rebuild_workspace_view_schema.func(str(setup.workspace.id))
    assert result["cube_schema"]["ok"] is True
    await setup.view.arefresh_from_db()
    assert setup.view.view_sources["views"][setup.dataset.table_name] == expected_source
    await _assert_real_count(setup, 3)


@pytest.mark.asyncio
async def test_successive_publications_keep_ascii_names_and_source_identity(published_sources):
    setup = published_sources
    original = deepcopy(setup.view.view_sources)
    ascii_prefix = "a" * 32
    assert f"{ascii_prefix}__raw_visits" in original["views"]
    assert view_name(ascii_prefix, "x" * 63) in original["views"]
    for _ in range(2):
        view = await sync_to_async(SchemaManager().build_view_schema)(setup.workspace)
        assert view.view_sources == original
    # A display-name change alone must not invalidate existing physical identity.
    await Tenant.objects.filter(id=setup.tenants[0].id).aupdate(canonical_name="Renamed")
    await _assert_real_count(setup, 2)


@pytest.mark.asyncio
async def test_failed_ddl_preserves_last_good_source_map(published_sources):
    setup = published_sources
    original = deepcopy(setup.view.view_sources)
    await sync_to_async(_create_table)(setup.dsn, setup.schemas[0].schema_name, "new_table")
    with patch.object(
        SchemaManager,
        "_create_readonly_role",
        side_effect=RuntimeError("Synthetic post-DDL failure"),
    ):
        with pytest.raises(RuntimeError, match="post-DDL"):
            await sync_to_async(SchemaManager().build_view_schema)(setup.workspace)
    await setup.view.arefresh_from_db()
    # The publication rolled back, so the last-good views keep serving: the row
    # stays ACTIVE with its prior provenance and only records why the rebuild failed.
    assert setup.view.state == SchemaState.ACTIVE
    assert "post-DDL" in setup.view.last_error
    assert setup.view.view_sources == original
    view = await sync_to_async(SchemaManager().build_view_schema)(setup.workspace)
    assert len(view.view_sources["views"]) == len(original["views"]) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "invalid", "foreign", "mixed"])
async def test_invalid_explicit_map_blocks_catalog_and_queries(published_sources, corruption):
    setup = published_sources
    value = deepcopy(setup.view.view_sources)
    if corruption == "missing":
        del value["views"][setup.dataset.table_name]
    elif corruption == "invalid":
        value = {"version": 99, "views": {}}
    elif corruption == "foreign":
        value["views"][setup.dataset.table_name]["tenant_id"] = str(uuid4())
    else:
        value["views"]["foreign__visits"] = {
            "tenant_id": str(uuid4()),
            "source_table_name": "raw_visits",
        }
    await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(view_sources=value)
    with pytest.raises(SemanticCatalogUnavailable):
        await sync_to_async(ensure_semantic_model)(setup.workspace)
    await setup.dataset.arefresh_from_db()
    assert setup.dataset.metadata["source_tenant_ids"] == [str(setup.tenants[0].id)]
    await _assert_recovery(setup, "view_rebuild")


@pytest.mark.asyncio
async def test_legacy_unresolved_view_blocks_until_safe_rebuild(published_sources):
    setup = published_sources
    await WorkspaceViewSchema.objects.filter(pk=setup.view.pk).aupdate(view_sources={})
    await SemanticDataset.objects.filter(pk=setup.dataset.pk).aupdate(metadata={})
    await _assert_recovery(setup, "view_rebuild")
    result = await rebuild_workspace_view_schema.func(str(setup.workspace.id))
    assert result["cube_schema"]["ok"] is True
    await _assert_real_count(setup, 2)


@pytest.mark.asyncio
async def test_provenance_does_not_widen_artifact_access(published_sources):
    setup = published_sources
    other_workspace = await Workspace.objects.acreate(name="Separate synthetic workspace")
    await WorkspaceTenant.objects.acreate(workspace=other_workspace, tenant=setup.tenants[0])
    await WorkspaceMembership.objects.acreate(
        workspace=other_workspace, user=setup.user, role=WorkspaceRole.MANAGE
    )
    stranger = await User.objects.acreate_user(
        email=f"stranger-{uuid4().hex}@example.com", password="test"
    )
    stranger_client = AsyncClient()
    await sync_to_async(_login)(stranger_client, stranger)
    wrong_scope = f"/api/workspaces/{other_workspace.id}/artifacts/{setup.artifact.id}/query-data/"
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as query:
        assert (await AsyncClient().get(setup.url)).status_code == 401
        assert (await stranger_client.get(setup.url)).status_code == 403
        assert (await setup.client.get(wrong_scope)).status_code == 404
        query.assert_not_awaited()
