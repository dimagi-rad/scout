"""A view rebuild publishes what the workspace is now, not what it was when queued."""

import os
import threading
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from django.conf import settings
from django.db import connection
from psycopg import sql

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager, ViewSchemaRetired
from apps.workspaces.services.tenant_coverage import coverage_entry
from apps.workspaces.services.workspace_service import add_workspace_tenant
from apps.workspaces.tasks import rebuild_workspace_view_schema

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"), reason="MANAGED_DATABASE_URL not set"
    ),
    pytest.mark.django_db(transaction=True),
]


def _managed(statement, params=None):
    with psycopg.connect(settings.MANAGED_DATABASE_URL, autocommit=True) as conn:
        cursor = conn.execute(statement, params)
        return cursor.fetchall() if cursor.description else None


def _schema_exists(name) -> bool:
    return bool(_managed("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,)))


@pytest.fixture
def two_live_sources(workspace, tenant):
    """A two-source workspace whose sources both serve a real table."""
    second = Tenant.objects.create(
        provider="commcare", external_id=f"race-{uuid4().hex[:8]}", canonical_name="Race Two"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=second)
    owned = []
    for source in (tenant, second):
        name = f"race_{uuid4().hex[:12]}"
        _managed(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        _managed(sql.SQL("CREATE TABLE {}.cases (id text)").format(sql.Identifier(name)))
        TenantSchema.objects.create(tenant=source, schema_name=name, state=SchemaState.ACTIVE)
        owned.append(name)
    view_schema = SchemaManager()._view_schema_name(workspace.id)
    yield workspace, second, view_schema
    for name in (view_schema, *owned):
        _managed(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.mark.parametrize("retired", [SchemaState.TEARDOWN, SchemaState.EXPIRED])
@pytest.mark.asyncio
async def test_a_rebuild_queued_before_retirement_leaves_the_row_retired(two_live_sources, retired):
    """#645 follow-up: a rebuild queued while the row was ACTIVE ran after the TTL
    sweep retired it and turned it ACTIVE again, so its teardown aborted."""
    workspace, _, view_schema = two_live_sources
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name=view_schema, state=retired
    )

    result = await rebuild_workspace_view_schema.func(str(workspace.id))

    assert result["status"] == "skipped"
    row = await WorkspaceViewSchema.objects.aget(workspace=workspace)
    assert row.state == retired
    assert not _schema_exists(view_schema)


@pytest.mark.asyncio
async def test_a_recovery_rebuild_still_revives_an_expired_view_schema(two_live_sources):
    workspace, _, view_schema = two_live_sources
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name=view_schema, state=SchemaState.EXPIRED
    )

    with patch("apps.workspaces.tasks.build_and_promote_cube_schema", MagicMock()):
        result = await rebuild_workspace_view_schema.func(str(workspace.id), revive_retired=True)

    assert result["status"] == "active"
    row = await WorkspaceViewSchema.objects.aget(workspace=workspace)
    assert row.state == SchemaState.ACTIVE
    assert _schema_exists(view_schema)


@pytest.mark.parametrize("retired", [SchemaState.TEARDOWN, SchemaState.EXPIRED])
def test_a_retirement_during_the_build_wins_over_publication(two_live_sources, retired):
    """Dropping to one source flips an ACTIVE row to TEARDOWN without W or T, so it
    can land while a rebuild's DDL runs; publishing ACTIVE over it revives the row."""
    workspace, _, view_schema = two_live_sources
    SchemaManager().build_view_schema(workspace)
    write_marker = SchemaManager._write_publication_marker

    def retire_mid_build(cursor, schema_name, build_token):
        write_marker(cursor, schema_name, build_token)
        WorkspaceViewSchema.objects.filter(workspace=workspace).update(state=retired)

    with (
        patch.object(SchemaManager, "_write_publication_marker", side_effect=retire_mid_build),
        pytest.raises(ViewSchemaRetired),
    ):
        SchemaManager().build_view_schema(workspace)

    assert WorkspaceViewSchema.objects.get(workspace=workspace).state == retired
    # Its teardown already ran, so nothing else would drop what this build created.
    assert _schema_exists(view_schema) is (retired == SchemaState.TEARDOWN)


@pytest.mark.parametrize("was_active", [True, False], ids=["rebuild", "first-build"])
def test_a_source_added_during_the_build_stays_named_missing(two_live_sources, user, was_active):
    """#637 follow-up: adding an unloaded source names it excluded at once, but a
    rebuild that planned before the add published coverage without it, so answers
    omitted the source silently until the add's own rebuild ran."""
    workspace, second, _ = two_live_sources
    first = workspace.tenants.exclude(id=second.id).get()
    if was_active:
        SchemaManager().build_view_schema(workspace)
    added = Tenant.objects.create(
        provider="commcare", external_id=f"race-{uuid4().hex[:8]}", canonical_name="Added"
    )
    write_marker = SchemaManager._write_publication_marker

    def add_mid_build(cursor, schema_name, build_token):
        write_marker(cursor, schema_name, build_token)
        with (
            patch("apps.workspaces.services.workspace_service.rebuild_workspace_view_schema"),
            patch("apps.workspaces.services.workspace_service.materialize_workspace"),
        ):
            add_workspace_tenant(workspace, added, actor_id=user.id)

    with patch.object(SchemaManager, "_write_publication_marker", side_effect=add_mid_build):
        SchemaManager().build_view_schema(workspace)

    coverage = WorkspaceViewSchema.objects.get(workspace=workspace).tenant_coverage
    included = sorted((coverage_entry(t) for t in (first, second)), key=lambda e: e["external_id"])
    assert sorted(coverage["included_tenants"], key=lambda e: e["external_id"]) == included
    assert coverage["excluded_tenants"] == [coverage_entry(added)]


def test_an_add_still_committing_when_the_first_build_publishes_is_named_missing(
    two_live_sources, user
):
    """The add's recording skips a row that is not ACTIVE yet, so publication must
    wait for the add's transaction rather than read the sources before it commits."""
    workspace, _, _ = two_live_sources
    added = Tenant.objects.create(
        provider="commcare", external_id=f"race-{uuid4().hex[:8]}", canonical_name="Added"
    )
    add_open = threading.Event()
    release_add = threading.Event()
    failures = []

    def hold_add_open(**_):
        add_open.set()
        assert release_add.wait(10)

    def add():
        try:
            with (
                patch("apps.workspaces.services.workspace_service.rebuild_workspace_view_schema"),
                patch(
                    "apps.workspaces.services.workspace_service.materialize_workspace.defer",
                    side_effect=hold_add_open,
                ),
            ):
                add_workspace_tenant(workspace, added, actor_id=user.id)
        except Exception as exc:
            failures.append(exc)
            add_open.set()
        finally:
            connection.close()

    adder = threading.Thread(target=add)
    write_marker = SchemaManager._write_publication_marker

    def add_mid_build(cursor, schema_name, build_token):
        write_marker(cursor, schema_name, build_token)
        adder.start()
        assert add_open.wait(10)
        threading.Timer(0.5, release_add.set).start()

    try:
        with patch.object(SchemaManager, "_write_publication_marker", side_effect=add_mid_build):
            SchemaManager().build_view_schema(workspace)
    finally:
        release_add.set()
        adder.join(10)

    assert not failures
    coverage = WorkspaceViewSchema.objects.get(workspace=workspace).tenant_coverage
    assert coverage["excluded_tenants"] == [coverage_entry(added)]
