"""A view rebuild publishes what the workspace is now, not what it was when queued."""

import os
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from django.conf import settings
from psycopg import sql

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager, ViewSchemaRetired
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
