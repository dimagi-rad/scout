"""Real managed-PostgreSQL proof of D2's publication invariants.

Nothing here mocks DDL: the views, grants, roles and commit marker are
exercised against the managed database configured by
``MANAGED_DATABASE_URL``, and every read that claims "still readable" is done
through the workspace's read-only role with ``SET ROLE``.

Schema and role names carry a uuid suffix so parallel runs sharing one managed
database never touch each other's objects.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import psycopg.errors
import psycopg.sql
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.common.identifiers import readonly_role_name, view_name
from apps.users.models import Tenant
from apps.workspaces import tasks
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services import schema_manager as sm
from apps.workspaces.services.schema_manager import (
    SchemaManager,
    get_managed_db_connection,
)
from tests.tenant_lock_probe import try_tenant_data_lock

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"),
        reason="MANAGED_DATABASE_URL not set",
    ),
]


class _OwnedSchemas:
    """Physical schemas this test created, dropped (with their roles) on teardown."""

    def __init__(self):
        self.names: list[str] = []

    def register(self, name: str) -> str:
        self.names.append(name)
        return name


@pytest.fixture
def managed():
    conn = get_managed_db_connection()
    yield conn
    if not conn.closed:
        conn.close()


@pytest.fixture
def owned(managed):
    registry = _OwnedSchemas()
    yield registry
    manager = SchemaManager()
    cursor = managed.cursor()
    for name in registry.names:
        with contextlib.suppress(Exception):
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(name)
                )
            )
    for name in registry.names:
        with contextlib.suppress(Exception):
            manager._drop_readonly_role(cursor, name)
            manager._drop_dbt_role(cursor, name)
    cursor.close()


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _seed_tenant_schema(conn, schema_name: str, *, sentinel: str, staging_view: bool = False):
    """Create a tenant schema the way provisioning does, with one sentinel row."""
    cursor = conn.cursor()
    cursor.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema_name)))
    SchemaManager()._create_readonly_role(cursor, schema_name)
    cursor.execute(
        psycopg.sql.SQL("CREATE TABLE {}.raw_cases (value text)").format(
            psycopg.sql.Identifier(schema_name)
        )
    )
    cursor.execute(
        psycopg.sql.SQL("INSERT INTO {}.raw_cases VALUES ({})").format(
            psycopg.sql.Identifier(schema_name), psycopg.sql.Literal(sentinel)
        )
    )
    if staging_view:
        # dbt materializes staging models inside the tenant schema; they depend on
        # the raw tables, so retirement has to drop them in dependency order.
        cursor.execute(
            psycopg.sql.SQL("CREATE VIEW {}.stg_cases AS SELECT * FROM {}.raw_cases").format(
                psycopg.sql.Identifier(schema_name), psycopg.sql.Identifier(schema_name)
            )
        )
    cursor.close()


def _make_workspace(tenants: list[Tenant]) -> Workspace:
    user = get_user_model().objects.create_user(
        email=f"d2-{_suffix()}@example.com", password="pass"
    )
    ws = Workspace.objects.create(name=f"D2 WS {_suffix()}", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    for tenant in tenants:
        WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


def _one_tenant_workspace(owned, managed, *, sentinel: str = "v1", staging_view: bool = False):
    """A workspace with one tenant whose physical schema holds ``sentinel``."""
    suffix = _suffix()
    tenant = Tenant.objects.create(
        provider="commcare", external_id=f"d2-{suffix}", canonical_name=f"d2a{suffix}"
    )
    schema_name = owned.register(f"d2_{suffix}")
    _seed_tenant_schema(managed, schema_name, sentinel=sentinel, staging_view=staging_view)
    tenant_schema = TenantSchema.objects.create(
        tenant=tenant, schema_name=schema_name, state=SchemaState.ACTIVE
    )
    workspace = _make_workspace([tenant])
    owned.register(SchemaManager()._view_schema_name(workspace.id))
    return workspace, tenant, tenant_schema


def _read_through_role(schema_name: str, relation: str) -> list[str]:
    """Read a view exactly as the workspace's read-only role would see it."""
    conn = get_managed_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            psycopg.sql.SQL("SET ROLE {}").format(
                psycopg.sql.Identifier(readonly_role_name(schema_name))
            )
        )
        cursor.execute(
            psycopg.sql.SQL("SELECT value FROM {}.{}").format(
                psycopg.sql.Identifier(schema_name), psycopg.sql.Identifier(relation)
            )
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def _relations(conn, schema_name: str) -> set[str]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'v')",
        (schema_name,),
    )
    names = {row[0] for row in cursor.fetchall()}
    cursor.close()
    return names


def _schema_exists(conn, schema_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema_name,))
    found = cursor.fetchone() is not None
    cursor.close()
    return found


def test_publication_commits_views_grants_and_marker(owned, managed):
    workspace, tenant, _ts = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()

    vs = manager.build_view_schema(workspace)

    assert vs.state == SchemaState.ACTIVE
    assert vs.physical_build_token
    assert manager.read_publication_marker(vs.schema_name) == vs.physical_build_token

    published_view = view_name(manager._view_prefix(tenant), "raw_cases")
    assert _relations(managed, vs.schema_name) == {published_view}
    assert _read_through_role(vs.schema_name, published_view) == ["v1"]
    assert vs.view_sources["views"][published_view]["tenant_id"] == str(tenant.id)
    assert [entry["tenant_id"] for entry in vs.tenant_coverage["included_tenants"]] == [
        str(tenant.id)
    ]
    assert manager.reconcile_view_publication(workspace) == {"status": "consistent"}


def test_failed_rebuild_of_active_row_keeps_serving_the_last_good_views(owned, managed):
    workspace, tenant, _ts = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    published_view = view_name(manager._view_prefix(tenant), "raw_cases")
    marker_before = manager.read_publication_marker(vs.schema_name)
    coverage_before = vs.tenant_coverage
    sources_before = vs.view_sources
    token_before = vs.physical_build_token

    # A table that only the failed rebuild would have published, so a surviving
    # schema cannot be mistaken for the new plan.
    managed.execute(
        psycopg.sql.SQL("CREATE TABLE {}.raw_visits (value text)").format(
            psycopg.sql.Identifier(_ts.schema_name)
        )
    )

    with (
        patch.object(
            SchemaManager, "_create_readonly_role", side_effect=RuntimeError("grant boom")
        ),
        pytest.raises(RuntimeError, match="grant boom"),
    ):
        manager.build_view_schema(workspace)

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE
    assert vs.tenant_coverage == coverage_before
    assert vs.view_sources == sources_before
    assert vs.physical_build_token == token_before
    assert "grant boom" in vs.last_error

    assert manager.read_publication_marker(vs.schema_name) == marker_before
    assert _relations(managed, vs.schema_name) == {published_view}
    assert _read_through_role(vs.schema_name, published_view) == ["v1"]


def test_failed_first_publication_leaves_no_physical_schema(owned, managed):
    workspace, _tenant, _ts = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    view_schema_name = manager._view_schema_name(workspace.id)

    with (
        patch.object(
            SchemaManager, "_create_readonly_role", side_effect=RuntimeError("grant boom")
        ),
        pytest.raises(RuntimeError, match="grant boom"),
    ):
        manager.build_view_schema(workspace)

    vs = WorkspaceViewSchema.objects.get(workspace=workspace)
    assert vs.state == SchemaState.FAILED
    assert "grant boom" in vs.last_error
    assert vs.physical_build_token == ""
    assert not _schema_exists(managed, view_schema_name)


def test_reconcile_republishes_a_commit_the_control_row_never_recorded(owned, managed):
    workspace, tenant, _ts = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    token_before = vs.physical_build_token
    original_save = WorkspaceViewSchema.save

    def die_after_commit(self, *args, **kwargs):
        if "physical_build_token" in (kwargs.get("update_fields") or ()):
            raise RuntimeError("worker died before recording the publication")
        return original_save(self, *args, **kwargs)

    with (
        patch.object(WorkspaceViewSchema, "save", die_after_commit),
        pytest.raises(RuntimeError, match="worker died"),
    ):
        manager.build_view_schema(workspace)

    orphaned_marker = manager.read_publication_marker(vs.schema_name)
    vs.refresh_from_db()
    assert orphaned_marker not in (None, token_before)
    assert vs.physical_build_token == token_before

    assert manager.reconcile_view_publication(workspace) == {
        "status": "republished",
        "reason": "marker_mismatch",
    }

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE
    assert vs.physical_build_token == manager.read_publication_marker(vs.schema_name)
    published_view = view_name(manager._view_prefix(tenant), "raw_cases")
    assert _read_through_role(vs.schema_name, published_view) == ["v1"]


def test_unreadable_marker_is_reported_as_no_evidence(owned, managed):
    schema_name = owned.register(f"d2mark_{_suffix()}")
    managed.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema_name)))
    manager = SchemaManager()

    assert manager.read_publication_marker(f"d2absent_{_suffix()}") is None
    assert manager.read_publication_marker(schema_name) is None

    managed.execute(
        psycopg.sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            psycopg.sql.Identifier(schema_name), psycopg.sql.Literal("not json")
        )
    )
    assert manager.read_publication_marker(schema_name) is None


def test_reconcile_republishes_when_the_physical_schema_disappeared(owned, managed):
    workspace, tenant, _ts = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    managed.execute(
        psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(vs.schema_name))
    )

    assert manager.reconcile_view_publication(workspace) == {
        "status": "republished",
        "reason": "physical_missing",
    }

    vs.refresh_from_db()
    published_view = view_name(manager._view_prefix(tenant), "raw_cases")
    assert vs.physical_build_token == manager.read_publication_marker(vs.schema_name)
    assert _read_through_role(vs.schema_name, published_view) == ["v1"]


@pytest.mark.asyncio
async def test_standalone_rebuild_holds_the_tenant_lock_through_grants(owned, managed):
    workspace, tenant, _old = await sync_to_async(_one_tenant_workspace)(owned, managed)
    manager = SchemaManager()
    vs = await sync_to_async(manager.build_view_schema)(workspace)
    await WorkspaceViewSchema.objects.filter(pk=vs.pk).aupdate(physical_build_token="lost")
    original = SchemaManager._create_readonly_role
    probes = []

    def grants(self, cursor, name):
        # Record rather than assert here: the task's broad except would hide it.
        with try_tenant_data_lock(tenant.id) as held:
            probes.append(held)
        return original(self, cursor, name)

    with (
        patch.object(SchemaManager, "_create_readonly_role", grants),
        patch.object(tasks, "_included_tenant_snapshot_state", AsyncMock(return_value="safe")),
        patch.object(tasks, "build_and_promote_cube_schema", return_value=MagicMock()),
    ):
        result = await tasks.rebuild_workspace_view_schema.func(str(workspace.id))
    assert result["status"] == "active"
    assert probes == [False], "Standalone publication must hold T through managed grants"
    await vs.arefresh_from_db()
    assert vs.physical_build_token == await sync_to_async(manager.read_publication_marker)(
        vs.schema_name
    )


def test_a_rebuild_keeps_an_active_row_out_of_the_inactivity_sweep(owned, managed):
    """PROVISIONING used to exclude a rebuilding row from expire_inactive_schemas;
    an ACTIVE row stays ACTIVE now, so the build must refresh its access time."""
    workspace, _tenant, _ts = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    stale = timezone.now() - timedelta(days=30)
    WorkspaceViewSchema.objects.filter(pk=vs.pk).update(last_accessed_at=stale)
    seen_during_build = []
    original = SchemaManager._create_readonly_role

    def grants(self, cursor, name):
        seen_during_build.append(
            WorkspaceViewSchema.objects.values_list("last_accessed_at", flat=True).get(pk=vs.pk)
        )
        return original(self, cursor, name)

    with patch.object(SchemaManager, "_create_readonly_role", grants):
        manager.build_view_schema(workspace)

    assert seen_during_build and seen_during_build[0] > stale + timedelta(days=29)


def test_failed_first_publication_records_tenant_coverage(owned, managed):
    workspace, tenant, _ts = _one_tenant_workspace(owned, managed)
    with (
        patch.object(
            SchemaManager, "_create_readonly_role", side_effect=RuntimeError("grant boom")
        ),
        pytest.raises(RuntimeError),
    ):
        SchemaManager().build_view_schema(workspace)

    vs = WorkspaceViewSchema.objects.get(workspace=workspace)
    assert vs.state == SchemaState.FAILED
    assert [e["tenant_id"] for e in vs.tenant_coverage["included_tenants"]] == [str(tenant.id)]


def _drop_view_like_an_in_place_load(managed, tenant_schema):
    """An in-place load's ``DROP TABLE raw_* CASCADE`` takes every view reading it."""
    managed.execute(
        psycopg.sql.SQL("DROP TABLE {}.raw_cases CASCADE").format(
            psycopg.sql.Identifier(tenant_schema.schema_name)
        )
    )
    managed.execute(
        psycopg.sql.SQL("CREATE TABLE {}.raw_cases (value text)").format(
            psycopg.sql.Identifier(tenant_schema.schema_name)
        )
    )


def test_failed_rebuild_after_views_were_dropped_underneath_is_not_reported_serving(owned, managed):
    """Rollback restores only what the build itself changed. If a load already
    cascaded the views away, the row must not keep claiming a serving layer."""
    workspace, _tenant, tenant_schema = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    _drop_view_like_an_in_place_load(managed, tenant_schema)

    with (
        patch.object(
            SchemaManager, "_create_readonly_role", side_effect=RuntimeError("grant boom")
        ),
        pytest.raises(RuntimeError, match="grant boom"),
    ):
        manager.build_view_schema(workspace)

    vs.refresh_from_db()
    assert vs.state == SchemaState.FAILED
    assert "grant boom" in vs.last_error


def test_reconcile_republishes_views_dropped_from_under_a_matching_marker(owned, managed):
    workspace, tenant, tenant_schema = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    _drop_view_like_an_in_place_load(managed, tenant_schema)
    assert manager.read_publication_marker(vs.schema_name) == vs.physical_build_token

    assert manager.reconcile_view_publication(workspace) == {
        "status": "republished",
        "reason": "views_missing",
    }
    published_view = view_name(manager._view_prefix(tenant), "raw_cases")
    assert published_view in _relations(managed, vs.schema_name)


def test_publication_waits_for_a_locked_source_then_rolls_back_to_last_good(
    owned, managed, monkeypatch
):
    """A load holding its raw table must not interleave with our DROP SCHEMA (the
    two lock orders form a cycle); the publication waits, times out, and keeps v1."""
    workspace, tenant, tenant_schema = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    published_view = view_name(manager._view_prefix(tenant), "raw_cases")
    monkeypatch.setattr(sm, "_PUBLICATION_LOCK_TIMEOUT", "200ms")

    loader = get_managed_db_connection()
    try:
        loader.autocommit = False
        loader.execute(
            psycopg.sql.SQL("LOCK TABLE {}.raw_cases IN ACCESS EXCLUSIVE MODE").format(
                psycopg.sql.Identifier(tenant_schema.schema_name)
            )
        )
        with pytest.raises(psycopg.errors.LockNotAvailable):
            manager.build_view_schema(workspace)
    finally:
        loader.rollback()
        loader.close()

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE
    assert _read_through_role(vs.schema_name, published_view) == ["v1"]


def test_reconcile_reports_a_failed_republish_instead_of_raising(owned, managed):
    """Callers use reconcile as a pre-check; a republish that cannot succeed (no
    source left to serve) must come back as a status, not abort them."""
    workspace, _tenant, tenant_schema = _one_tenant_workspace(owned, managed)
    manager = SchemaManager()
    vs = manager.build_view_schema(workspace)
    managed.execute(
        psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(vs.schema_name))
    )
    TenantSchema.objects.filter(pk=tenant_schema.pk).update(state=SchemaState.TEARDOWN)

    result = manager.reconcile_view_publication(workspace)

    assert result["status"] == "republish_failed"
    assert result["reason"] == "physical_missing"
    vs.refresh_from_db()
    assert vs.state == SchemaState.FAILED
