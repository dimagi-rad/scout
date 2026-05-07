"""Tests for the MCP run_materialization tool's progress-formatting helpers."""

from unittest.mock import AsyncMock, patch

import pytest
from procrastinate.jobs import Status as ProcrastinateStatus

from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from mcp_server.server import _format_progress_message, _query_workspace_progress


def test_format_progress_with_total():
    msg = _format_progress_message(
        {
            "message": "Loading sessions from OCS API...",
            "rows_loaded": 500,
            "rows_total": 13028,
            "source": "sessions",
        },
        multi_tenant=False,
    )
    assert msg == "Loading sessions from OCS API... 3.8% (500 / 13,028 rows)"


def test_format_progress_without_total_falls_back_to_count():
    msg = _format_progress_message(
        {
            "message": "Loading sessions from OCS API...",
            "rows_loaded": 500,
            "rows_total": None,
            "source": "sessions",
        },
        multi_tenant=False,
    )
    assert msg == "Loading sessions from OCS API... 500 rows loaded"


def test_format_progress_non_load_phase():
    msg = _format_progress_message(
        {
            "message": "Provisioning schema for my-experiment...",
            "rows_loaded": 0,
            "rows_total": None,
            "source": None,
        },
        multi_tenant=False,
    )
    assert msg == "Provisioning schema for my-experiment..."


def test_format_progress_multi_tenant_prefixes_tenant_id():
    msg = _format_progress_message(
        {
            "message": "Loading sessions from OCS API...",
            "rows_loaded": 100,
            "rows_total": 200,
            "source": "sessions",
            "tenant_id": "exp-1",
        },
        multi_tenant=True,
    )
    assert msg.startswith("[exp-1] ")
    assert "100 / 200 rows" in msg


def test_format_progress_handles_missing_message():
    msg = _format_progress_message(
        {"rows_loaded": 0, "rows_total": None},
        multi_tenant=False,
    )
    assert msg == "Working..."


# ---------------------------------------------------------------------------
# _query_workspace_progress: procrastinate-job-state backstop
#
# The worker may skip memberships (no pipeline / no credential) without
# creating a MaterializationRun row. Without this backstop, the poller
# would never see ``completed_runs >= expected_count`` and would time out.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_query_workspace_progress_no_runs_job_finished_marks_done(workspace):
    """No run rows + procrastinate job finished = every membership was skipped;
    poll loop must exit, not wait."""
    with patch(
        "mcp_server.server._procrastinate_app.job_manager.get_job_status_async",
        new_callable=AsyncMock,
        return_value=ProcrastinateStatus.SUCCEEDED,
    ):
        result = await _query_workspace_progress(
            workspace_id=str(workspace.id), job_id=42, expected_count=2
        )

    assert result["all_done"] is True
    assert result["queued"] is False
    assert result["tenants"] == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_query_workspace_progress_no_runs_job_running_stays_queued(workspace):
    """No run rows yet but the job is still running — poller should keep waiting."""
    with patch(
        "mcp_server.server._procrastinate_app.job_manager.get_job_status_async",
        new_callable=AsyncMock,
        return_value=ProcrastinateStatus.DOING,
    ):
        result = await _query_workspace_progress(
            workspace_id=str(workspace.id), job_id=42, expected_count=1
        )

    assert result["all_done"] is False
    assert result["queued"] is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_query_workspace_progress_partial_runs_job_finished_marks_done(
    db, workspace, tenant
):
    """``expected_count`` is 2 (memberships) but only one run row exists because
    the worker skipped one membership. The job has finished — the poller
    must accept ``all_done`` instead of waiting for the missing row."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_partial", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=42,
        result={"sources": {}},
    )

    with patch(
        "mcp_server.server._procrastinate_app.job_manager.get_job_status_async",
        new_callable=AsyncMock,
        return_value=ProcrastinateStatus.SUCCEEDED,
    ):
        result = await _query_workspace_progress(
            workspace_id=str(workspace.id), job_id=42, expected_count=2
        )

    assert result["all_done"] is True
    assert len(result["tenants"]) == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_query_workspace_progress_partial_runs_job_running_keeps_waiting(
    db, workspace, tenant
):
    """Same shape but the job is still running — must NOT short-circuit."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_partial_running", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=42,
        result={"sources": {}},
    )

    with patch(
        "mcp_server.server._procrastinate_app.job_manager.get_job_status_async",
        new_callable=AsyncMock,
        return_value=ProcrastinateStatus.DOING,
    ):
        result = await _query_workspace_progress(
            workspace_id=str(workspace.id), job_id=42, expected_count=2
        )

    assert result["all_done"] is False
