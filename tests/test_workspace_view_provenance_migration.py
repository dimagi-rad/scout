"""Real PostgreSQL rolling-write compatibility for the additive provenance field."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext

from apps.workspaces.models import WorkspaceViewSchema
from apps.workspaces.services.view_sources import parse_view_sources

BEFORE = ("workspaces", "0011_workspacedatarecovery")
AFTER = ("workspaces", "0012_workspaceviewschema_view_sources")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def old_view_model(transactional_db):
    """Rewind only the owned test DB, and restore all migration leaves on failure."""
    assert connection.vendor == "postgresql"
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old_apps = _migrate([BEFORE])
        yield (
            old_apps.get_model("workspaces", "WorkspaceViewSchema"),
            old_apps.get_model("workspaces", "Workspace"),
        )
    finally:
        _migrate(leaves)


def test_preexisting_rows_and_old_model_writes_survive_provenance_migration(old_view_model):
    old_view, old_workspace = old_view_model
    assert "view_sources" not in {field.name for field in old_view._meta.concrete_fields}
    workspace = old_workspace.objects.create(name="Pre-migration workspace")
    existing = old_view.objects.create(
        workspace=workspace, schema_name="ws_before_provenance", state="active"
    )

    # Forward to the current schema: the current model must read rows the
    # pre-0012 writer created, and that writer must keep inserting without any
    # column added since (every later addition needs a real database default).
    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
    assert WorkspaceViewSchema.objects.get(pk=existing.pk).view_sources == {}
    existing.last_error = "An older worker can still update this row"
    existing.save()  # Includes only fields known to the pre-0012 model.
    current = WorkspaceViewSchema.objects.get(pk=existing.pk)
    assert current.last_error == existing.last_error
    assert current.view_sources == {}

    with CaptureQueriesContext(connection) as queries:
        added = old_view.objects.create(
            workspace=old_workspace.objects.create(name="Old process after migration"),
            schema_name="ws_old_process_after_migration",
            state="provisioning",
        )
    old_insert = next(
        query["sql"]
        for query in queries
        if 'INSERT INTO "workspaces_workspaceviewschema"' in query["sql"]
    )
    assert "view_sources" not in old_insert
    current = WorkspaceViewSchema.objects.get(pk=added.pk)
    assert current.view_sources == {}
    assert parse_view_sources(current.view_sources, set()) is None

    # An old writer's normal update does not erase provenance already published
    # by the new code, even though that writer cannot select the new field.
    published = {"version": 1, "views": {}}
    current.view_sources = published
    current.save(update_fields=["view_sources"])
    added.state = "active"
    added.save()
    current.refresh_from_db()
    assert current.state == "active"
    assert current.view_sources == published


def test_provenance_migration_keeps_an_actual_postgresql_default(old_view_model):
    with CaptureQueriesContext(connection) as queries:
        _migrate([AFTER])
    emitted_sql = "\n".join(query["sql"] for query in queries)
    assert 'ADD COLUMN "view_sources" jsonb' in emitted_sql
    assert 'ALTER COLUMN "view_sources" DROP DEFAULT' not in emitted_sql

    # Read the installed PostgreSQL schema, not Django's Python-side default.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'workspaces_workspaceviewschema'
              AND column_name = 'view_sources'
            """
        )
        assert cursor.fetchone() == ("'{}'::jsonb", "NO")


def test_current_model_has_no_unrecorded_migration_changes(db):
    output = StringIO()
    call_command("makemigrations", check=True, dry_run=True, stdout=output)
    assert "No changes detected" in output.getvalue()
