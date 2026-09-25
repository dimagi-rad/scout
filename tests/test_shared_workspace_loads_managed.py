"""Last-good data through a shared load, on real control and managed PostgreSQL.

Two multi-tenant workspaces share tenant S. Workspace A reloads S into a
candidate. While that load runs, both workspaces still read v1; after A's load
publishes, A reads v2 immediately and B reads v1 until its own rebuild, and the
old schema is only retired once nothing reads it. Only the provider fetch
(which writes the sentinel into the candidate) and the Cube build are stubbed.
"""

import contextlib
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import psycopg.sql
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.common.identifiers import readonly_role_name, view_name
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
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection
from tests.pipeline_doubles import completed_pipeline_run
from tests.tenant_access import grant_tenant_access

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
    ),
]


def _exec(sql, *params):
    with get_managed_db_connection() as conn:
        conn.execute(sql, params or None)


def _write_sentinel(schema_name: str, value: str) -> None:
    ident = psycopg.sql.Identifier(schema_name)
    _exec(psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident))
    _exec(psycopg.sql.SQL("DROP TABLE IF EXISTS {}.raw_cases").format(ident))
    _exec(psycopg.sql.SQL("CREATE TABLE {}.raw_cases (value text)").format(ident))
    _exec(
        psycopg.sql.SQL("INSERT INTO {}.raw_cases VALUES ({})").format(
            ident, psycopg.sql.Literal(value)
        )
    )


def _read(view_schema: str, relation: str) -> list[str]:
    with get_managed_db_connection() as conn:
        conn.execute(
            psycopg.sql.SQL("SET ROLE {}").format(
                psycopg.sql.Identifier(readonly_role_name(view_schema))
            )
        )
        rows = conn.execute(
            psycopg.sql.SQL("SELECT value FROM {}.{}").format(
                psycopg.sql.Identifier(view_schema), psycopg.sql.Identifier(relation)
            )
        ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture
def owned_names():
    names: list[str] = []
    yield names
    manager = SchemaManager()
    with get_managed_db_connection() as conn:
        cursor = conn.cursor()
        for name in names:
            with contextlib.suppress(Exception):
                cursor.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(name)
                    )
                )
        for name in names:
            with contextlib.suppress(Exception):
                manager._drop_readonly_role(cursor, name)
                manager._drop_dbt_role(cursor, name)


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
        _write_sentinel(schema, f"{key}1")
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
            during_load.append((_read(view_a, shared_view), _read(view_b, shared_view)))
            _write_sentinel(target_schema.schema_name, "s2")
        else:
            _write_sentinel(target_schema.schema_name, "a2")
        return completed_pipeline_run(membership, credential, pipeline, job_id, target_schema)

    with (
        patch("apps.workspaces.tasks._run_pipeline_with_progress", side_effect=fetch),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            AsyncMock(return_value={"type": "api_key", "value": "k"}),
        ),
        patch(
            "apps.workspaces.tasks.build_and_promote_cube_schema",
            return_value=MagicMock(id="cube", content_hash="hash"),
        ),
        patch("apps.workspaces.tasks._rebuild_dependent_view_schemas", AsyncMock()),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retire,
        patch(
            "apps.workspaces.tasks._included_tenant_snapshot_state", AsyncMock(return_value="safe")
        ),
    ):
        retire.return_value.defer_async = AsyncMock(return_value=1)
        result = await workspaces_tasks.materialize_workspace_core(
            str(workspaces["A"].id), str(user.id)
        )

    assert result["all_succeeded"] is True
    assert during_load == [(["s1"], ["s1"])]
    await old_shared.arefresh_from_db()
    assert old_shared.state == SchemaState.TEARDOWN
    # A published and rebuilt against the new schema; B still reads last-good.
    assert await sync_to_async(_read)(view_a, shared_view) == ["s2"]
    assert await sync_to_async(_read)(view_b, shared_view) == ["s1"]

    with patch("apps.workspaces.tasks.teardown_schema.configure") as retry:
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await workspaces_tasks.teardown_schema(schema_id=str(old_shared.id))
    await old_shared.arefresh_from_db()
    assert old_shared.state == SchemaState.TEARDOWN, "B's views still read the old schema"

    with patch(
        "apps.workspaces.tasks.build_and_promote_cube_schema",
        return_value=MagicMock(id="cube", content_hash="hash"),
    ):
        await workspaces_tasks.rebuild_workspace_view_schema.func(str(workspaces["B"].id))
    assert await sync_to_async(_read)(view_b, shared_view) == ["s2"]

    await workspaces_tasks.teardown_schema(schema_id=str(old_shared.id), attempt=1)
    await old_shared.arefresh_from_db()
    assert old_shared.state == SchemaState.EXPIRED
