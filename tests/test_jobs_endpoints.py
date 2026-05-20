from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_returns_pending_job_with_progress():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE,
    )
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t1", provider="commcare", canonical_name="Test Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant, schema_name="s_t1",
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=1001, tool_call_id="tc1",
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=1001,
        progress={
            "step": 3, "total_steps": 5,
            "source": "cases", "message": "Loading cases...",
            "rows_loaded": 64000, "rows_total": 100000,
            "run_id": str(schema.id),
        },
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    j = body["jobs"][0]
    assert j["thread_id"] == str(thread.id)
    assert j["state"] == "pending"
    assert j["progress"]["percent"] == 64
    assert j["progress"]["rows_loaded"] == 64000


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_empty_when_none_running():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE,
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_unauthenticated_returns_401_or_403():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    client = AsyncClient()
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_non_member_blocked():
    owner = await sync_to_async(User.objects.create_user)(email="o@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=owner)
    await sync_to_async(User.objects.create_user)(email="out@b.c", password="x")
    client = AsyncClient()
    await sync_to_async(client.login)(email="out@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_flips_state_and_aborts_procrastinate():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE,
    )
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t1", provider="commcare", canonical_name="Test Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t1")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=2002, tool_call_id="tc2",
        state=ThreadJob.State.RUNNING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=2002,
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")

    with patch(
        "apps.workspaces.api.jobs_cancel.current_app"
    ) as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=None)
        resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.CANCELLED
    run = await MaterializationRun.objects.aget(procrastinate_job_id=2002)
    assert run.state == MaterializationRun.RunState.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_cross_user_blocked():
    owner = await sync_to_async(User.objects.create_user)(email="o@b.c", password="x")
    other = await sync_to_async(User.objects.create_user)(email="x@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=owner)
    # Both users are workspace members so aresolve_workspace lets them through.
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=owner, role=WorkspaceRole.READ_WRITE,
    )
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=other, role=WorkspaceRole.READ,
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=owner)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=3030, tool_call_id="tcX",
        state=ThreadJob.State.RUNNING,
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="x@b.c", password="x")
    resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 404
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.RUNNING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_double_cancel_is_idempotent():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE,
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=4040, tool_call_id="tc4",
        state=ThreadJob.State.CANCELLED,
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")
    resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_terminal", "state": "cancelled"}
