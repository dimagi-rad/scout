from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceTenant,
)
from mcp_server.server import run_materialization

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_returns_started_immediately_and_creates_threadjob():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t1", provider="commcare", canonical_name="Test Tenant"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)

    job_mock = MagicMock(id=7777)
    with patch("mcp_server.server.materialize_workspace") as mw:
        mw.defer_async = AsyncMock(return_value=job_mock)
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            thread_id=str(thread.id),
            tool_call_id="tc-xyz",
        )

    assert result["data"]["status"] == "started"
    assert "thread_job_id" in result["data"]
    tj = await ThreadJob.objects.aget(procrastinate_job_id=7777)
    assert tj.thread_id == thread.id
    assert tj.tool_call_id == "tc-xyz"
    assert tj.state == ThreadJob.State.PENDING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_rolls_back_dispatch_when_threadjob_create_fails():
    """If ThreadJob.acreate raises after the procrastinate job has been
    deferred, the procrastinate job is aborted and an error envelope is
    returned. Documents that the rollback path is wired correctly even if
    its best-effort nature is acceptable per the design."""
    user = await sync_to_async(User.objects.create_user)(email="b@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W2", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t2", provider="commcare", canonical_name="Test Tenant 2"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)

    job_mock = MagicMock(id=8888)
    cancel_mock = AsyncMock(return_value=None)

    with patch("mcp_server.server.materialize_workspace") as mw, \
         patch("mcp_server.server.ThreadJob.objects.acreate", side_effect=Exception("DB down")), \
         patch("mcp_server.server._procrastinate_app") as proc_app:
        mw.defer_async = AsyncMock(return_value=job_mock)
        proc_app.job_manager.cancel_job_by_id_async = cancel_mock
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            thread_id=str(thread.id),
            tool_call_id="tc-rollback",
        )

    # Error envelope: {"success": False, "error": {"code": ..., "message": ...}}
    assert result["success"] is False
    assert result["error"]["code"] == "INTERNAL_ERROR"
    # No ThreadJob persisted for this procrastinate job id
    assert not await ThreadJob.objects.filter(procrastinate_job_id=8888).aexists()
    # Rollback abort was attempted
    cancel_mock.assert_awaited_once_with(8888, abort=True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_rejects_thread_owned_by_other_user():
    """Defense-in-depth check: the MCP tool rejects a thread_id that doesn't
    belong to (user_id, workspace_id) even if the chat-layer guard somehow
    failed."""
    user_a = await sync_to_async(User.objects.create_user)(email="usera@b.c", password="x")
    user_b = await sync_to_async(User.objects.create_user)(email="userb@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-cross", created_by=user_a)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="tx", provider="commcare", canonical_name="X Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user_a)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user_b)
    foreign_thread = await sync_to_async(Thread.objects.create)(
        workspace=ws, user=user_a,
    )

    result = await run_materialization(
        workspace_id=str(ws.id),
        user_id=str(user_b.id),  # user_b calls, but thread belongs to user_a
        thread_id=str(foreign_thread.id),
        tool_call_id="tc-cross",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "NOT_FOUND"
    # Confirm no ThreadJob was created for the foreign thread
    assert not await ThreadJob.objects.filter(thread_id=foreign_thread.id).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_returns_already_in_progress_if_active_in_same_thread():
    """If a materialization is already running in THIS chat thread, do not
    dispatch a duplicate — return the existing thread_job_id."""
    user = await sync_to_async(User.objects.create_user)(email="dup@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-dup", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="tdup", provider="commcare", canonical_name="Dup Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user)

    # Same thread holds the in-progress job and is also the caller.
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    existing_tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=11111, tool_call_id="tc-existing",
        state=ThreadJob.State.PENDING,
    )

    result = await run_materialization(
        workspace_id=str(ws.id),
        user_id=str(user.id),
        thread_id=str(thread.id),
        tool_call_id="tc-new",
    )

    assert result["data"]["status"] == "already_in_progress"
    assert result["data"]["thread_job_id"] == str(existing_tj.id)
    # No new ThreadJob created for this thread
    assert await ThreadJob.objects.filter(thread=thread).acount() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_allows_dispatch_from_different_thread_in_same_workspace():
    """The dedupe guard is scoped per-thread (not per-workspace) so a parallel
    chat session in the same workspace can run its own materialization. The
    chained resume task only fires once per ThreadJob, so a workspace-scoped
    guard would leave the second caller's spinner hanging forever."""
    user = await sync_to_async(User.objects.create_user)(email="parallel@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-parallel", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="tparallel", provider="commcare", canonical_name="Parallel Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(TenantMembership.objects.create)(tenant=tenant, user=user)

    # Thread 1 has an in-progress job.
    thread1 = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    await sync_to_async(ThreadJob.objects.create)(
        thread=thread1, job_type="materialization",
        procrastinate_job_id=22222, tool_call_id="tc-thread1",
        state=ThreadJob.State.PENDING,
    )

    # Thread 2 is a different chat — should be allowed to dispatch its own.
    thread2 = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)

    job_mock = MagicMock(id=33333)
    with patch("mcp_server.server.materialize_workspace") as mw:
        mw.defer_async = AsyncMock(return_value=job_mock)
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            thread_id=str(thread2.id),
            tool_call_id="tc-thread2",
        )

    assert result["data"]["status"] == "started"
    # Two ThreadJobs in this workspace now (one per thread).
    assert await ThreadJob.objects.filter(thread__workspace_id=ws.id).acount() == 2
    new_tj = await ThreadJob.objects.aget(procrastinate_job_id=33333)
    assert new_tj.thread_id == thread2.id
