"""Last-good data through a shared load, on real control and managed PostgreSQL.

Two multi-tenant workspaces share tenant S. Workspace A reloads S into a
candidate. While that load runs, both workspaces still read v1; after A's load
publishes, A reads v2 immediately and B reads v1 until its own rebuild, and the
old schema is only retired once nothing reads it. When a retirement's drop fails
and reverts the schema, the siblings it moved off are put back onto it. Only the provider fetch
(which writes the sentinel into the candidate) and the Cube build are stubbed.
"""

import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import psycopg.sql
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

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
)
from apps.workspaces.services import materialize
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
    user = get_user_model().objects.create_user(email=f"shared-{suffix}@example.com")
    tenants = {}
    for key in ("s", "a", "b"):
        tenant = Tenant.objects.create(
            provider="commcare", external_id=f"{key}-{suffix}", canonical_name=f"{key}{suffix}"
        )
        grant_tenant_access(user, tenant)
        schema = f"sl_{key}_{suffix}"
        owned_names.append(schema)
        with get_managed_db_connection() as conn:
            conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
            SchemaManager()._create_readonly_role(conn.cursor(), schema)
        write_sentinel(schema, f"{key}1")
        TenantSchema.objects.create(tenant=tenant, schema_name=schema, state=SchemaState.ACTIVE)
        tenants[key] = tenant
    workspaces = {}
    for key, own in (("A", "a"), ("B", "b")):
        ws = Workspace.objects.create(name=f"{key}{suffix}", created_by=user)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
        for tenant in (tenants["s"], tenants[own]):
            WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
        owned_names.append(SchemaManager()._view_schema_name(ws.id))
        SchemaManager().build_view_schema(ws)
        workspaces[key] = ws
    return user, tenants, workspaces


async def test_siblings_read_last_good_through_a_shared_load_until_they_rebuild(owned_names):
    user, tenants, workspaces = await sync_to_async(_setup)(owned_names)
    manager = SchemaManager()
    shared_view = view_name(manager._view_prefix(tenants["s"]), "raw_cases")
    view_a = manager._view_schema_name(workspaces["A"].id)
    view_b = manager._view_schema_name(workspaces["B"].id)
    old_shared = await TenantSchema.objects.aget(tenant=tenants["s"], state=SchemaState.ACTIVE)
    during_load = []

    def fetch(membership, credential, pipeline, job_id, target_schema=None):
        owned_names.append(target_schema.schema_name)
        if membership.tenant_id == tenants["s"].id:
            during_load.append(
                (read_as_readonly(view_a, shared_view), read_as_readonly(view_b, shared_view))
            )
            write_sentinel(target_schema.schema_name, "s2")
        else:
            write_sentinel(target_schema.schema_name, "a2")
        return completed_pipeline_run(membership, credential, pipeline, job_id, target_schema)

    with (
        patch(
            "apps.workspaces.services.materialize._run_pipeline_with_progress", side_effect=fetch
        ),
        patch(
            "apps.workspaces.services.materialize.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch(
            "apps.workspaces.services.materialize.build_and_promote_cube_schema",
            return_value=MagicMock(id="cube", content_hash="hash"),
        ),
        patch("apps.workspaces.services.publication.rebuild_dependent_view_schemas", AsyncMock()),
        patch("apps.workspaces.services.retirement.configure_teardown_schema") as retire,
        patch(
            "apps.workspaces.services.materialize._included_tenant_snapshot_state",
            AsyncMock(return_value="safe"),
        ),
    ):
        retire.return_value.defer_async = AsyncMock(return_value=1)
        result = await materialize.materialize_workspace_core(str(workspaces["A"].id), str(user.id))

    assert result["all_succeeded"] is True
    assert during_load == [(["s1"], ["s1"])]
    await old_shared.arefresh_from_db()
    assert old_shared.state == SchemaState.TEARDOWN
    # A published and rebuilt against the new schema; B still reads last-good.
    assert await sync_to_async(read_as_readonly)(view_a, shared_view) == ["s2"]
    assert await sync_to_async(read_as_readonly)(view_b, shared_view) == ["s1"]

    with patch("apps.workspaces.services.retirement.configure_teardown_schema") as retry:
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.teardown_schema(schema_id=str(old_shared.id))
    await old_shared.arefresh_from_db()
    assert old_shared.state == SchemaState.TEARDOWN, "B's views still read the old schema"

    with patch(
        "apps.workspaces.services.publication.build_and_promote_cube_schema",
        return_value=MagicMock(id="cube", content_hash="hash"),
    ):
        await workspaces_tasks.rebuild_workspace_view_schema.func(str(workspaces["B"].id))
    assert await sync_to_async(read_as_readonly)(view_b, shared_view) == ["s2"]

    await workspaces_tasks.teardown_schema(schema_id=str(old_shared.id), attempt=1)
    await old_shared.arefresh_from_db()
    assert old_shared.state == SchemaState.EXPIRED


async def test_a_reverted_retirement_rebuilds_siblings_back_onto_the_active_schema(owned_names):
    _, tenants, workspaces = await sync_to_async(_setup)(owned_names)
    manager = SchemaManager()
    shared_view = view_name(manager._view_prefix(tenants["s"]), "raw_cases")
    view_schemas = {key: manager._view_schema_name(ws.id) for key, ws in workspaces.items()}
    shared = await TenantSchema.objects.aget(tenant=tenants["s"], state=SchemaState.ACTIVE)
    expired_at = timezone.now() - timedelta(days=30)
    shared.state = SchemaState.TEARDOWN
    shared.last_accessed_at = expired_at
    await shared.asave(update_fields=["state", "last_accessed_at"])
    cube = patch(
        "apps.workspaces.services.publication.build_and_promote_cube_schema",
        return_value=MagicMock(id="cube", content_hash="hash"),
    )

    # TTL expiry: the first attempt finds both siblings reading S and moves them off it.
    with (
        patch("apps.workspaces.services.retirement.configure_teardown_schema") as retry,
        patch(
            "apps.workspaces.services.retirement.adefer_rebuild_workspace_view_schema", AsyncMock()
        ),
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.teardown_schema(schema_id=str(shared.id))
    with cube:
        for ws in workspaces.values():
            await workspaces_tasks.rebuild_workspace_view_schema.func(str(ws.id))
    with pytest.raises(psycopg.errors.UndefinedTable):
        await sync_to_async(read_as_readonly)(view_schemas["A"], shared_view)

    deferred = AsyncMock()
    with (
        patch.object(SchemaManager, "retire_tenant_schema", side_effect=RuntimeError("boom")),
        patch(
            "apps.workspaces.services.publication.adefer_rebuild_workspace_view_schema", deferred
        ),
        pytest.raises(RuntimeError),
    ):
        await workspaces_tasks.teardown_schema(schema_id=str(shared.id), attempt=1)
    await shared.arefresh_from_db()
    assert shared.state == SchemaState.ACTIVE
    assert shared.last_accessed_at > expired_at, "the next sweep must not re-expire it at once"

    rebuilt = sorted(call.kwargs["workspace_id"] for call in deferred.await_args_list)
    assert rebuilt == sorted(str(ws.id) for ws in workspaces.values())
    with cube:
        for workspace_id in rebuilt:
            await workspaces_tasks.rebuild_workspace_view_schema.func(workspace_id)
    for view_schema in view_schemas.values():
        assert await sync_to_async(read_as_readonly)(view_schema, shared_view) == ["s1"]
