"""The 0007 backfill classifies WorkspaceViewSchema rows that predate last_error_code.

Unlike MaterializationRun.result — always read job-scoped, so never historical —
these rows are one per workspace and outlive deploys. A pre-existing
FAILED-by-cascade row that backfilled as a build failure would tell the user a
system-side fix is required when re-materializing is in fact the fix (07#9).
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as global_apps

from apps.common.error_codes import ErrorCode
from apps.workspaces.models import SchemaState, Workspace, WorkspaceViewSchema

_migration = importlib.import_module(
    "apps.workspaces.migrations.0007_workspaceviewschema_last_error_code"
)


def _view_schema(name: str, *, state: str, last_error: str) -> WorkspaceViewSchema:
    workspace = Workspace.objects.create(name=name)
    return WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name=f"ws_{name}",
        state=state,
        last_error=last_error,
    )


@pytest.mark.django_db
def test_backfill_classifies_legacy_rows_by_the_retired_marker():
    cascaded = _view_schema(
        "cascaded",
        state=SchemaState.FAILED,
        last_error="[cascade-teardown] A tenant schema this workspace's view depends on…",
    )
    build_failed = _view_schema(
        "build-failed",
        state=SchemaState.FAILED,
        last_error='relation "ws_x.raw_visits" does not exist',
    )
    healthy = _view_schema("healthy", state=SchemaState.ACTIVE, last_error="")

    _migration.backfill_last_error_code(global_apps, None)

    for row in (cascaded, build_failed, healthy):
        row.refresh_from_db()
    assert cascaded.last_error_code == ErrorCode.VIEW_SCHEMA_CASCADE_TEARDOWN
    assert build_failed.last_error_code == ErrorCode.SCHEMA_BUILD_FAILED
    # An ACTIVE row has no error to classify; inventing a code would make
    # `last_error_code` truthy for a healthy schema.
    assert healthy.last_error_code == ""


@pytest.mark.django_db
def test_backfill_leaves_an_already_coded_row_alone():
    """Idempotent, and it must not downgrade a cascade written by the new code."""
    row = _view_schema("coded", state=SchemaState.FAILED, last_error="cascade-dropped, no sentinel")
    WorkspaceViewSchema.objects.filter(pk=row.pk).update(
        last_error_code=ErrorCode.VIEW_SCHEMA_CASCADE_TEARDOWN
    )

    _migration.backfill_last_error_code(global_apps, None)

    row.refresh_from_db()
    assert row.last_error_code == ErrorCode.VIEW_SCHEMA_CASCADE_TEARDOWN
