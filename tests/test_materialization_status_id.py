from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from mcp_server.server import get_materialization_status, mcp

User = get_user_model()


def test_status_tool_declares_injected_actor_context():
    tool = mcp._tool_manager.get_tool("get_materialization_status")
    declared = set(tool.fn_metadata.arg_model.model_fields)

    assert {"workspace_id", "user_id", "thread_id"}.issubset(declared)


async def _make_materialization_job(*, email: str, job_id: int):
    user = await User.objects.acreate_user(email=email, password="x")
    workspace = await Workspace.objects.acreate(name=f"Workspace {job_id}", created_by=user)
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    job = await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=job_id,
        tool_call_id=f"tool-{job_id}",
    )
    return user, workspace, job


async def _add_tenant_run(
    *,
    workspace: Workspace,
    job: ThreadJob,
    external_id: str,
    state: str,
    progress: dict,
):
    tenant = await Tenant.objects.acreate(
        external_id=external_id,
        provider="commcare",
        canonical_name=f"Tenant {external_id}",
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    tenant_schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name=f"schema_{external_id}",
        state=SchemaState.ACTIVE,
    )
    return await MaterializationRun.objects.acreate(
        tenant_schema=tenant_schema,
        pipeline="commcare_sync",
        state=state,
        progress=progress,
        procrastinate_job_id=job.procrastinate_job_id,
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_job_id_returns_pending_status_before_runs_exist():
    user, workspace, job = await _make_materialization_job(
        email="pending-status@example.com",
        job_id=71001,
    )

    result = await get_materialization_status(
        run_id=str(job.id),
        workspace_id=str(workspace.id),
        user_id=str(user.id),
    )

    assert result["success"] is True
    assert result["schema"] == ""
    assert result["data"]["thread_job_id"] == str(job.id)
    assert result["data"]["state"] == ThreadJob.State.PENDING
    assert result["data"]["runs"] == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_job_id_retains_each_active_tenant_run_and_progress():
    user, workspace, job = await _make_materialization_job(
        email="multi-status@example.com",
        job_id=71002,
    )
    first_run = await _add_tenant_run(
        workspace=workspace,
        job=job,
        external_id="status-tenant-a",
        state=MaterializationRun.RunState.LOADING,
        progress={"current": 20, "total": 100, "stage": "cases"},
    )
    second_run = await _add_tenant_run(
        workspace=workspace,
        job=job,
        external_id="status-tenant-b",
        state=MaterializationRun.RunState.TRANSFORMING,
        progress={"current": 80, "total": 100, "stage": "dbt"},
    )

    result = await get_materialization_status(
        run_id=str(job.id),
        workspace_id=str(workspace.id),
        user_id=str(user.id),
    )

    assert result["success"] is True
    runs_by_tenant = {run["tenant_id"]: run for run in result["data"]["runs"]}
    assert set(runs_by_tenant) == {"status-tenant-a", "status-tenant-b"}
    assert runs_by_tenant["status-tenant-a"]["run_id"] == str(first_run.id)
    assert runs_by_tenant["status-tenant-a"]["state"] == MaterializationRun.RunState.LOADING
    assert runs_by_tenant["status-tenant-a"]["progress"] == {
        "current": 20,
        "total": 100,
        "stage": "cases",
    }
    assert runs_by_tenant["status-tenant-b"]["run_id"] == str(second_run.id)
    assert runs_by_tenant["status-tenant-b"]["state"] == MaterializationRun.RunState.TRANSFORMING
    assert runs_by_tenant["status-tenant-b"]["progress"] == {
        "current": 80,
        "total": 100,
        "stage": "dbt",
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_job_id_is_not_found_for_another_user_or_workspace():
    owner, workspace, job = await _make_materialization_job(
        email="status-owner@example.com",
        job_id=71003,
    )
    other_user = await User.objects.acreate_user(email="status-other@example.com", password="x")
    other_workspace = await Workspace.objects.acreate(name="Other workspace", created_by=owner)

    wrong_user_result = await get_materialization_status(
        run_id=str(job.id),
        workspace_id=str(workspace.id),
        user_id=str(other_user.id),
    )
    wrong_workspace_result = await get_materialization_status(
        run_id=str(job.id),
        workspace_id=str(other_workspace.id),
        user_id=str(owner.id),
    )

    assert wrong_user_result["success"] is False
    assert wrong_user_result["error"]["code"] == "NOT_FOUND"
    assert wrong_workspace_result["success"] is False
    assert wrong_workspace_result["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_raw_materialization_run_id_preserves_existing_response():
    user, workspace, job = await _make_materialization_job(
        email="raw-status@example.com",
        job_id=71004,
    )
    run = await _add_tenant_run(
        workspace=workspace,
        job=job,
        external_id="status-raw-tenant",
        state=MaterializationRun.RunState.COMPLETED,
        progress={"current": 100, "total": 100},
    )

    result = await get_materialization_status(
        run_id=str(run.id),
        workspace_id=str(workspace.id),
        user_id=str(user.id),
    )

    assert result["success"] is True
    assert result["schema"] == "schema_status-raw-tenant"
    assert result["data"] == {
        "run_id": str(run.id),
        "pipeline": "commcare_sync",
        "state": MaterializationRun.RunState.COMPLETED,
        "result": None,
        "started_at": run.started_at.isoformat(),
        "completed_at": None,
        "tenant_id": "status-raw-tenant",
    }
