"""Writer/reader contract for ``MaterializationRun.result``.

``result`` is an unschematized JSON blob: the materializer writes it, and
``get_schema_status``, the catalog, the resume prompt and the failure summary
all read it. ``get_schema_status`` had drifted off it — it indexed
``result["tables"]``, ``result["table"]`` and ``result["rows_loaded"]``, the
pre-#12 single-table shape that the per-source ``sources`` map replaced, so a
single-tenant workspace full of data was always reported as having no tables
(arch review 03#4).

The blob under test is *captured from the real writer* rather than hand-written,
so a rename on the writer side fails these tests instead of quietly deadening a
reader again.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.server import get_schema_status
from mcp_server.services.materializer import run_pipeline

PATCH_LIVE_TABLES = "mcp_server.services.metadata._live_tables_in_schema"

# Keys ``get_schema_status`` used to read out of ``result``. Kept as a literal so
# re-adding a reader for any of them without a matching writer fails here.
PRE_PIPELINE_RESULT_KEYS = ("tables", "table", "rows_loaded")


def _capture_writer_result() -> dict:
    """Run the real materializer and return the ``result`` blob it persists.

    Everything the pipeline touches outside the run record is mocked, but the
    ``result`` construction is the production code path — which is the point:
    the readers below are asserted against what the writer actually emits.
    """
    pipeline = PipelineConfig(
        name="commcare_sync",
        description="",
        version="1.0",
        provider="commcare",
        sources=[SourceConfig(name="cases", description="CommCare case records")],
    )

    schema = MagicMock()
    schema.schema_name = "t_contract"
    tenant_membership = MagicMock()
    tenant_membership.tenant.external_id = "contract-domain"

    with (
        patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
        patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
        patch("mcp_server.services.materializer.TenantMetadata"),
        patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
        patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
        patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
    ):
        mock_mgr.return_value.provision.return_value = schema
        run = MagicMock()
        run.id = "run-contract"
        mock_run_cls.objects.create.return_value = run
        for attr in (
            "DISCOVERING",
            "LOADING",
            "TRANSFORMING",
            "COMPLETED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
            "STALE",
        ):
            setattr(mock_run_cls.RunState, attr, attr.lower())
        mock_run_cls.ACTIVE_STATES = frozenset(
            {"started", "discovering", "loading", "transforming"}
        )
        mock_meta.return_value.load.return_value = {
            "app_definitions": [],
            "case_types": [],
            "form_definitions": {},
        }
        mock_cases.return_value.load_pages.return_value = iter([([{"case_id": "c1"}], 1)])
        mock_asset_cls.objects.filter.return_value.exists.return_value = False
        conn = MagicMock()
        mock_conn.return_value = conn
        conn.cursor.return_value = MagicMock()

        run_pipeline(tenant_membership, {"type": "api_key", "value": "x"}, pipeline)

        completed = [
            call.kwargs
            for call in mock_run_cls.objects.filter.return_value.update.call_args_list
            if call.kwargs.get("state") == "completed"
        ]

    assert len(completed) == 1, f"expected one COMPLETED write, got {len(completed)}"
    return completed[0]["result"]


def test_writer_does_not_emit_the_keys_get_schema_status_used_to_read():
    """The dead keys are dead: the writer records loaded data under ``sources``."""
    result = _capture_writer_result()

    assert "sources" in result
    assert result["sources"]["cases"]["state"] == "completed"
    for key in PRE_PIPELINE_RESULT_KEYS:
        assert key not in result, f"writer emits {key!r} — reader expectations need revisiting"


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.django_db(transaction=True)
async def test_get_schema_status_reports_tables_for_single_tenant_workspace():
    """A loaded single-tenant workspace must not be reported as empty (03#4)."""
    result_blob = _capture_writer_result()

    User = get_user_model()
    user = await User.objects.acreate_user(email="contract@b.c", password="x")
    workspace = await Workspace.objects.acreate(name="W-contract", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="contract-domain",
        provider="commcare",
        canonical_name="Contract Domain",
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    tenant_schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="t_contract",
        state=SchemaState.ACTIVE,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=tenant_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result=result_blob,
        completed_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC),
    )

    with patch(PATCH_LIVE_TABLES, AsyncMock(return_value={"raw_cases"})):
        response = await get_schema_status(workspace_id=str(workspace.id))

    assert response["success"] is True
    data = response["data"]
    assert data["exists"] is True
    assert data["state"] == SchemaState.ACTIVE
    assert data["last_materialized_at"] == "2026-09-08T12:00:00+00:00"

    tables = {t["name"]: t for t in data["tables"]}
    assert "raw_cases" in tables, "loaded source missing from get_schema_status"
    assert tables["raw_cases"]["materialized_row_count"] == 1
    assert tables["raw_cases"]["row_count_verified"] is False
