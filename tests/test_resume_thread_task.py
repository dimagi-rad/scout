from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_appends_system_message_and_invokes_agent():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t1", provider="commcare", canonical_name="Test Tenant"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t1")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=3003, tool_call_id="tc3",
        state=ThreadJob.State.RUNNING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=3003,
        result={"rows": 50000},
    )

    from apps.workspaces.tasks import resume_thread_after_materialization

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(
            None, thread_job_id=str(tj.id),
        )

    assert result["status"] == "resumed"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.COMPLETED
    # Inspect the input_state passed to ainvoke.
    call_args = mock_agent.ainvoke.await_args
    input_state = call_args.args[0]
    messages = input_state["messages"]
    assert len(messages) == 1
    assert messages[0].content.startswith("[__system_resume__]")
    assert "completed" in messages[0].content
    # Confirm oauth_tokens is forwarded into the runtime config.
    config = call_args.args[1]
    assert "oauth_tokens" in config


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_is_idempotent_when_already_claimed():
    """If the resume task is dispatched twice and the first claim wins, the
    second invocation no-ops."""
    user = await sync_to_async(User.objects.create_user)(email="b@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W2", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t2", provider="commcare", canonical_name="Test Tenant 2",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t2")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=4040, tool_call_id="tc4",
        state=ThreadJob.State.COMPLETED,  # already terminal
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=4040,
    )

    from apps.workspaces.tasks import resume_thread_after_materialization

    result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))
    assert result["status"] == "already_terminal"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_no_runs_marks_threadjob_failed_without_invoking_agent():
    """If somehow there are no MaterializationRun rows for the procrastinate
    job, the resume short-circuits to FAILED and does not invoke the agent."""
    user = await sync_to_async(User.objects.create_user)(email="c@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W3", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=5050, tool_call_id="tc5",
        state=ThreadJob.State.RUNNING,
    )
    # No MaterializationRun rows for procrastinate_job_id=5050.

    from apps.workspaces.tasks import resume_thread_after_materialization

    # _build_agent_for_resume must NOT be invoked.
    with patch("apps.workspaces.tasks._build_agent_for_resume") as build:
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))
        build.assert_not_called()

    assert result["status"] == "no_runs"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_partial_maps_to_failed():
    """A run still in LOADING state when resume fires yields 'partial' status,
    which should map to ThreadJob.State.FAILED."""
    user = await sync_to_async(User.objects.create_user)(email="d@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W4", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t4", provider="commcare", canonical_name="Test Tenant 4",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t4")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=6060, tool_call_id="tc6",
        state=ThreadJob.State.RUNNING,
    )
    # One run still in LOADING — triggers "partial" aggregate status.
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=6060,
    )

    from apps.workspaces.tasks import resume_thread_after_materialization

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "resumed"
    assert result["terminal_state"] == ThreadJob.State.FAILED
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_invokes_agent_for_cancelled_threadjob():
    """Per Option A, a cancelled ThreadJob still triggers an agent response so
    the user gets a graceful follow-up message."""
    user = await sync_to_async(User.objects.create_user)(email="f@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-cancel-resume", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-cancel", provider="commcare", canonical_name="Cancel Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant, schema_name="s_cancel",
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=7777, tool_call_id="tc-cancel",
        state=ThreadJob.State.CANCELLED,  # Cancelled BEFORE resume runs
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.CANCELLED,
        procrastinate_job_id=7777,
    )

    from apps.workspaces.tasks import resume_thread_after_materialization

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(
            None, thread_job_id=str(tj.id),
        )

    assert result["status"] == "resumed"
    mock_agent.ainvoke.assert_awaited_once()
    # Confirm the system-resume message mentions cancellation
    call_args = mock_agent.ainvoke.await_args
    body = call_args.args[0]["messages"][0].content
    assert "cancelled" in body.lower()
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_bumps_thread_updated_at_on_success():
    """After a successful resume, Thread.updated_at is updated so the
    sidebar's green-dot indicator can fire (lastUpdated > lastViewed)."""

    user = await sync_to_async(User.objects.create_user)(email="e@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W5", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t5", provider="commcare", canonical_name="Test Tenant 5"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t5")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=7070, tool_call_id="tc7",
        state=ThreadJob.State.RUNNING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=7070,
        result={"rows": 1000},
    )

    # Record the updated_at before the resume runs
    pre_resume_updated_at = thread.updated_at

    from apps.workspaces.tasks import resume_thread_after_materialization

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "resumed"

    # Thread.updated_at must have been bumped after the successful resume
    await sync_to_async(thread.refresh_from_db)()
    assert thread.updated_at > pre_resume_updated_at
