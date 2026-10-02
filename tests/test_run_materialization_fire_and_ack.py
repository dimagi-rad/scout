from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.signals import pre_save

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from mcp_server.server import _THIS_CONVERSATION_RESUMES, run_materialization
from tests.tenant_access import ausable_connection

User = get_user_model()
DISPATCH = "mcp_server.server.adispatch_thread_materialization"
QUEUE = "apps.workspaces.services.thread_job_dispatch.materialize_workspace"


async def _grant_manage(workspace, user):
    await WorkspaceMembership.objects.acreate(
        workspace=workspace, user=user, role=WorkspaceRole.MANAGE
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_observes_artifact_recovery(workspace, user):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=workspace,
        requested_by=user,
        recovery_type="semantic_rebuild",
    )
    with patch(DISPATCH, new=AsyncMock()) as dispatch:
        result = await run_materialization(
            workspace_id=str(workspace.id), user_id=str(user.id), thread_id=str(thread.id)
        )
    assert result["data"]["status"] == "already_in_progress"
    assert result["data"]["workspace_recovery_id"] == str(recovery.id)
    assert "Nothing will resume this conversation" in result["data"]["message"]
    assert _THIS_CONVERSATION_RESUMES not in result["data"]["message"]
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_rejects_non_uuid_thread_id():
    """A non-UUID thread_id (e.g. the recipe runner's synthetic
    "recipe-run-<id>") must fail cleanly with a validation error instead of
    raising ValueError out of the Thread UUIDField cast. Regression guard for
    the recipe materialization crash (SCOUT-DJANGO-1R/1S)."""
    user = await User.objects.acreate_user(email="nonuuid@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-nonuuid", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="tnu", provider="commcare", canonical_name="NonUUID Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )

    result = await run_materialization(
        workspace_id=str(ws.id),
        user_id=str(user.id),
        thread_id="recipe-run-f3be369b-d867-4ef8-aa0d-a74f21101c18",
        tool_call_id="tc-nonuuid",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert "thread" in result["error"]["message"].lower()
    # Must not have raised, and must not have created any ThreadJob.
    assert not await ThreadJob.objects.aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_returns_started_immediately_and_creates_threadjob():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t1", provider="commcare", canonical_name="Test Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    await _grant_manage(ws, user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)

    with patch(QUEUE) as mw:
        mw.defer = MagicMock(return_value=7777)
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            thread_id=str(thread.id),
            tool_call_id="tc-xyz",
        )

    assert result["data"]["status"] == "started"
    assert "thread_job_id" in result["data"]
    assert _THIS_CONVERSATION_RESUMES in result["data"]["message"]
    # Intent is captured before queueing, so an equivalent pending load is joined.
    assert mw.defer.call_args.kwargs["load_intent"] == {str(tenant.id): 1}
    tj = await ThreadJob.objects.aget(procrastinate_job_id=7777)
    assert tj.thread_id == thread.id
    assert tj.tool_call_id == "tc-xyz"
    assert tj.state == ThreadJob.State.PENDING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_rejects_thread_owned_by_other_user():
    """Defense-in-depth check: the MCP tool rejects a thread_id that doesn't
    belong to (user_id, workspace_id) even if the chat-layer guard somehow
    failed."""
    user_a = await User.objects.acreate_user(email="usera@b.c", password="x")
    user_b = await User.objects.acreate_user(email="userb@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-cross", created_by=user_a)
    tenant = await Tenant.objects.acreate(
        external_id="tx",
        provider="commcare",
        canonical_name="X Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user_a, tenant=tenant, connection=await ausable_connection(user_a, tenant.provider)
    )
    await TenantMembership.objects.acreate(
        user=user_b, tenant=tenant, connection=await ausable_connection(user_b, tenant.provider)
    )
    await _grant_manage(ws, user_b)
    foreign_thread = await Thread.objects.acreate(
        workspace=ws,
        user=user_a,
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
    user = await User.objects.acreate_user(email="dup@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-dup", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="tdup",
        provider="commcare",
        canonical_name="Dup Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    await _grant_manage(ws, user)

    # Same thread holds the in-progress job and is also the caller.
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    existing_tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=11111,
        tool_call_id="tc-existing",
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
    assert _THIS_CONVERSATION_RESUMES in result["data"]["message"]
    # No new ThreadJob created for this thread
    assert await ThreadJob.objects.filter(thread=thread).acount() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_allows_dispatch_from_different_thread_in_same_workspace():
    """The dedupe guard is scoped per-thread (not per-workspace) so a parallel
    chat session in the same workspace can run its own materialization. The
    chained resume task only fires once per ThreadJob, so a workspace-scoped
    guard would leave the second caller's spinner hanging forever."""
    user = await User.objects.acreate_user(email="parallel@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-parallel", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="tparallel",
        provider="commcare",
        canonical_name="Parallel Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    await _grant_manage(ws, user)

    # Thread 1 has an in-progress job.
    thread1 = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread1,
        job_type="materialization",
        procrastinate_job_id=22222,
        tool_call_id="tc-thread1",
        state=ThreadJob.State.PENDING,
    )

    # Thread 2 is a different chat — should be allowed to dispatch its own.
    thread2 = await Thread.objects.acreate(workspace=ws, user=user)

    with patch(QUEUE) as mw:
        mw.defer = MagicMock(return_value=33333)
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


def _visible_to_a_worker(job_id) -> bool:
    """Whether another connection, as a worker's would, can already see the job."""
    settings = connection.settings_dict
    with psycopg.connect(
        host=settings["HOST"] or None,
        port=settings["PORT"] or None,
        dbname=settings["NAME"],
        user=settings["USER"],
        password=settings["PASSWORD"],
        autocommit=True,
    ) as other:
        return (
            other.execute("SELECT 1 FROM procrastinate_jobs WHERE id = %s", [job_id]).fetchone()
            is not None
        )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_hides_the_job_from_workers_until_its_threadjob_exists():
    """#365: the job was committed before its ThreadJob, so a worker could finish it
    while the resume lookup still found no ThreadJob, and the chat never resumed."""
    user = await User.objects.acreate_user(email="race@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-race", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="trace", provider="commcare", canonical_name="Race Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    await _grant_manage(ws, user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)

    seen_before_threadjob = []

    def record(sender, instance, **kwargs):
        seen_before_threadjob.append(_visible_to_a_worker(instance.procrastinate_job_id))

    pre_save.connect(record, sender=ThreadJob)
    try:
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            thread_id=str(thread.id),
            tool_call_id="tc-race",
        )
    finally:
        pre_save.disconnect(record, sender=ThreadJob)

    assert result["data"]["status"] == "started"
    assert seen_before_threadjob == [False]
    tj = await ThreadJob.objects.aget(id=result["data"]["thread_job_id"])
    assert await sync_to_async(_visible_to_a_worker)(tj.procrastinate_job_id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_queues_nothing_when_its_threadjob_cannot_be_saved():
    user = await User.objects.acreate_user(email="rollback@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-rollback", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="trollback", provider="commcare", canonical_name="Rollback Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    await _grant_manage(ws, user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    queued = []

    def fail_after_queueing(sender, instance, **kwargs):
        queued.append(instance.procrastinate_job_id)
        raise RuntimeError("DB down")

    pre_save.connect(fail_after_queueing, sender=ThreadJob)
    try:
        result = await run_materialization(
            workspace_id=str(ws.id),
            user_id=str(user.id),
            thread_id=str(thread.id),
            tool_call_id="tc-rollback",
        )
    finally:
        pre_save.disconnect(fail_after_queueing, sender=ThreadJob)

    assert result["success"] is False
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert len(queued) == 1
    assert not await sync_to_async(_visible_to_a_worker)(queued[0])
    assert not await ThreadJob.objects.filter(thread=thread).aexists()
