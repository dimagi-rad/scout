"""Adding a never-loaded source loads it, on real control and managed PostgreSQL (#362).

A manager adds a source nobody has loaded to a workspace whose other two sources
serve data. The add queues a view rebuild and then a load of only the new source.
Run in queue order, the rebuild keeps the existing sources serving and names the
new one missing; the load then fetches it and republishes the views and Cube with
it. Only the provider fetch (which writes a sentinel) and the Cube build are stubbed.
"""

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import psycopg.sql
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.common.identifiers import view_name
from apps.users.models import Tenant
from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection
from tests.managed_sentinels import drop_owned, read_as_readonly, write_sentinel
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import grant_tenant_access

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
    ),
]


@pytest.fixture
def owned_names():
    names: list[str] = []
    yield names
    drop_owned(names)


def _setup(owned_names):
    suffix = uuid.uuid4().hex[:8]
    user = get_user_model().objects.create_user(email=f"add-{suffix}@example.com")
    ws = Workspace.objects.create(name=f"add{suffix}", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    tenants = {}
    for key in ("a", "b", "new"):
        tenant = Tenant.objects.create(
            provider="commcare", external_id=f"{key}-{suffix}", canonical_name=f"{key}{suffix}"
        )
        grant_tenant_access(user, tenant)
        tenants[key] = tenant
    for key in ("a", "b"):
        schema = f"as_{key}_{suffix}"
        owned_names.append(schema)
        with get_managed_db_connection() as conn:
            conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
            SchemaManager()._create_readonly_role(conn.cursor(), schema)
        write_sentinel(schema, f"{key}1")
        TenantSchema.objects.create(
            tenant=tenants[key], schema_name=schema, state=SchemaState.ACTIVE
        )
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenants[key])
    owned_names.append(SchemaManager()._view_schema_name(ws.id))
    SchemaManager().build_view_schema(ws)
    return user, ws, tenants


def _add_source(user, ws, tenant):
    client = APIClient()
    client.force_login(user)
    return client.post(
        f"/api/workspaces/{ws.id}/tenants/", {"tenant_id": str(tenant.id)}, format="json"
    )


def _coverage_ids(vs, key):
    return sorted(e["tenant_id"] for e in vs.tenant_coverage[key])


async def test_adding_an_unloaded_source_loads_it_and_republishes_the_views(owned_names):
    user, ws, tenants = await sync_to_async(_setup)(owned_names)
    manager = SchemaManager()
    view_schema = manager._view_schema_name(ws.id)
    relation = {key: view_name(manager._view_prefix(t), "raw_cases") for key, t in tenants.items()}
    serving = sorted(str(tenants[k].id) for k in ("a", "b"))

    queued = MagicMock()
    with (
        patch(
            "apps.workspaces.services.workspace_service.defer_rebuild_workspace_view_schema",
            queued.rebuild,
        ),
        patch(
            "apps.workspaces.services.workspace_service.defer_materialize_workspace", queued.load
        ),
    ):
        resp = await sync_to_async(_add_source)(user, ws, tenants["new"])

    assert resp.status_code == 202
    # The rebuild is queued ahead of the load, so coverage names the new source
    # missing without waiting out the load.
    assert [name for name, _args, _kwargs in queued.mock_calls] == ["rebuild", "load"]
    load_kwargs = queued.load.call_args.kwargs
    assert load_kwargs["only_unserved"] is True
    assert load_kwargs["user_id"] == str(user.id)

    cube = MagicMock(return_value=MagicMock(id="cube", content_hash="hash"))
    with patch("apps.workspaces.services.publication.build_and_promote_cube_schema", cube):
        await workspaces_tasks.rebuild_workspace_view_schema.func(**queued.rebuild.call_args.kwargs)

    vs = await WorkspaceViewSchema.objects.aget(workspace=ws)
    assert vs.state == SchemaState.ACTIVE
    assert _coverage_ids(vs, "included_tenants") == serving
    assert _coverage_ids(vs, "excluded_tenants") == [str(tenants["new"].id)]
    assert await sync_to_async(read_as_readonly)(view_schema, relation["a"]) == ["a1"]
    with pytest.raises(psycopg.errors.UndefinedTable):
        await sync_to_async(read_as_readonly)(view_schema, relation["new"])

    fetched = []

    def fetch(membership, credential, pipeline, job_id, target_schema=None):
        owned_names.append(target_schema.schema_name)
        fetched.append(membership.tenant_id)
        write_sentinel(target_schema.schema_name, "new1")
        return completed_pipeline_run(membership, credential, pipeline, job_id, target_schema)

    cube.reset_mock()
    fallback_rebuild = AsyncMock()
    with (
        patch(
            "apps.workspaces.services.materialize._run_pipeline_with_progress", side_effect=fetch
        ),
        patch(
            "apps.workspaces.services.materialize.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch("apps.workspaces.services.materialize.build_and_promote_cube_schema", cube),
        patch("apps.workspaces.services.publication.rebuild_dependent_view_schemas", AsyncMock()),
        patch(
            "apps.workspaces.services.materialize._included_tenant_snapshot_state",
            AsyncMock(return_value="safe"),
        ),
        patch(
            "apps.workspaces.services.materialize.adefer_rebuild_workspace_view_schema",
            fallback_rebuild,
        ),
    ):
        result = await workspaces_tasks.materialize_workspace(
            MagicMock(job=MagicMock(id=362)), **load_kwargs
        )

    assert result["all_succeeded"] is True
    assert fetched == [tenants["new"].id], "sources already serving are not reloaded"
    fallback_rebuild.assert_not_awaited()
    cube.assert_called()
    await vs.arefresh_from_db()
    assert vs.state == SchemaState.ACTIVE
    assert _coverage_ids(vs, "included_tenants") == sorted([*serving, str(tenants["new"].id)])
    assert vs.tenant_coverage["excluded_tenants"] == []
    assert await sync_to_async(read_as_readonly)(view_schema, relation["new"]) == ["new1"]
    assert await sync_to_async(read_as_readonly)(view_schema, relation["a"]) == ["a1"]
    assert await sync_to_async(read_as_readonly)(view_schema, relation["b"]) == ["b1"]
