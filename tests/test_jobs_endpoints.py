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
