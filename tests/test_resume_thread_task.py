from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from apps.workspaces.tasks import resume_thread_after_materialization

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
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=3003,
        tool_call_id="tc3",
        state=ThreadJob.State.PENDING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=3003,
        result={"rows": 50000},
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(
            None,
            thread_job_id=str(tj.id),
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
        external_id="t2",
        provider="commcare",
        canonical_name="Test Tenant 2",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t2")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=4040,
        tool_call_id="tc4",
        state=ThreadJob.State.COMPLETED,  # already terminal
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=4040,
    )

    result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))
    assert result["status"] == "already_terminal"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_no_runs_still_invokes_agent_with_explanation():
    """no_runs case: agent IS invoked so the user gets a useful message
    rather than being left with a frozen spinner, but the ThreadJob is
    still marked FAILED for observability."""
    user = await sync_to_async(User.objects.create_user)(email="c@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W3", created_by=user)
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=5050,
        tool_call_id="tc5",
        state=ThreadJob.State.PENDING,
    )
    # No MaterializationRun rows for procrastinate_job_id=5050.

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    # Agent IS invoked — user must get a message
    mock_agent.ainvoke.assert_awaited_once()
    body = mock_agent.ainvoke.await_args.args[0]["messages"][0].content
    assert any(
        phrase in body.lower()
        for phrase in ("no pipelines", "no_runs", "no credentials", "pipeline configured")
    )

    assert result["status"] == "resumed"
    assert result["terminal_state"] == ThreadJob.State.FAILED
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
        external_id="t4",
        provider="commcare",
        canonical_name="Test Tenant 4",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_t4")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=6060,
        tool_call_id="tc6",
        state=ThreadJob.State.PENDING,
    )
    # One run still in LOADING — triggers "partial" aggregate status.
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=6060,
    )

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
        external_id="t-cancel",
        provider="commcare",
        canonical_name="Cancel Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant,
        schema_name="s_cancel",
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=7777,
        tool_call_id="tc-cancel",
        state=ThreadJob.State.CANCELLED,  # Cancelled BEFORE resume runs
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.CANCELLED,
        procrastinate_job_id=7777,
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(
            None,
            thread_job_id=str(tj.id),
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
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=7070,
        tool_call_id="tc7",
        state=ThreadJob.State.PENDING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=7070,
        result={"rows": 1000},
    )

    # Record the updated_at before the resume runs
    pre_resume_updated_at = thread.updated_at

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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_does_not_clobber_concurrent_cancel_during_ainvoke():
    """If the user clicks Stop during agent.ainvoke (a 30s+ operation), the
    cancel endpoint writes ThreadJob.state=CANCELLED to the DB. When ainvoke
    returns, the resume task must NOT overwrite that with a success terminal.

    We simulate the race by having the mocked ainvoke flip the DB state
    inside its body — this is the same sequence the cancel endpoint would
    produce while the worker is blocked on the LLM call."""
    user = await sync_to_async(User.objects.create_user)(email="race@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-race", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-race",
        provider="commcare",
        canonical_name="Race Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_race")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=8484,
        tool_call_id="tc-race",
        state=ThreadJob.State.PENDING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=8484,
    )

    async def flip_to_cancelled_then_return(*args, **kwargs):
        # Simulate the cancel endpoint landing while ainvoke is mid-flight.
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.CANCELLED,
        )
        return {"messages": []}

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=flip_to_cancelled_then_return)
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    # Race-safe write must NOT clobber CANCELLED back to COMPLETED.
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.CANCELLED
    # The return value should report the *actual* state, not the value we
    # would have written.
    assert result["terminal_state"] == ThreadJob.State.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_does_not_force_cancelled_status_when_runs_completed():
    """Finding #9: if the user clicks Stop AFTER MaterializationRuns have
    finished but BEFORE the resume task runs, the in-memory tj.state is
    CANCELLED but the actual runs are COMPLETED — the data IS loaded. The
    agent message must reflect the truth (status=completed), not the user's
    racing intent (which would falsely say 'cancelled' and abandon the
    request)."""
    user = await sync_to_async(User.objects.create_user)(email="stale@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-stale", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-stale",
        provider="commcare",
        canonical_name="Stale Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_stale")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=9595,
        tool_call_id="tc-stale",
        # ThreadJob got flipped to CANCELLED by a late Stop click,
        # but the runs already completed before the cancel landed.
        state=ThreadJob.State.CANCELLED,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=9595,
        result={"rows": 1234},
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    # The system-resume body must say "completed" — the data is loaded.
    mock_agent.ainvoke.assert_awaited_once()
    body = mock_agent.ainvoke.await_args.args[0]["messages"][0].content
    assert "completed" in body.lower()
    assert "cancelled" not in body.lower()
    # Terminal state should be COMPLETED to match reality.
    assert result["terminal_state"] == ThreadJob.State.COMPLETED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_partial_run_surfaces_per_source_state_in_prompt():
    """A PARTIAL MaterializationRun must surface per-source state in the
    resume prompt so the agent can disclose what failed.
    """
    user = await sync_to_async(User.objects.create_user)(email="psrc@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-psrc", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-psrc",
        provider="commcare_connect",
        canonical_name="Conn Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant,
        schema_name="s_psrc",
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=12345,
        tool_call_id="tc-psrc",
        state=ThreadJob.State.PENDING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_connect",
        state=MaterializationRun.RunState.PARTIAL,
        procrastinate_job_id=12345,
        result={
            "pipeline": "commcare_connect",
            "sources": {
                "users": {"state": "completed", "rows": 100},
                "visits": {"state": "completed", "rows": 98869},
                "completed_works": {
                    "state": "failed",
                    "rows": 0,
                    "error": "RuntimeError: Connect 500",
                },
                "payments": {"state": "skipped", "rows": 0},
            },
        },
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    mock_agent.ainvoke.assert_awaited_once()
    body = mock_agent.ainvoke.await_args.args[0]["messages"][0].content
    # The prompt must call out PARTIAL state and include per-source detail.
    assert "PARTIAL" in body or "partial" in body
    # Per-source state must be present so the agent knows what's queryable.
    assert "completed_works" in body
    assert "failed" in body
    # And the resume terminal state for partial is FAILED (matches existing behavior).
    assert result["terminal_state"] == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_cas_rejects_already_running_threadjob():
    """If a ThreadJob is already in RUNNING state (a concurrent resume
    claimed it first), a second invocation must NOT proceed to ainvoke."""
    user = await sync_to_async(User.objects.create_user)(email="cas@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-cas", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-cas",
        provider="commcare",
        canonical_name="CAS Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_cas")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=24680,
        tool_call_id="tc-cas",
        state=ThreadJob.State.RUNNING,  # already-running; simulates a concurrent claim
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=24680,
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(
            None,
            thread_job_id=str(tj.id),
        )

    assert result["status"] == "already_claimed"
    mock_agent.ainvoke.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_recursion_limit_is_lowered_from_default():
    """Regression pin for issue #190: the resume path's recursion_limit must
    be lower than the chat default (50) so a panic-looping agent gets cut
    off before it can run 18+ tool calls."""
    user = await sync_to_async(User.objects.create_user)(email="rl@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W-rl", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-rl", provider="commcare", canonical_name="RL Tenant"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(tenant=tenant, schema_name="s_rl")
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=9999,
        tool_call_id="tc-rl",
        state=ThreadJob.State.PENDING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=9999,
        result={"rows": 100},
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    config = mock_agent.ainvoke.await_args.args[1]
    # Must read from the AGENT_RESUME_RECURSION_LIMIT setting (so ops can
    # tune it without code change) and must be lower than the chat default
    # of 50.
    assert config["recursion_limit"] == dj_settings.AGENT_RESUME_RECURSION_LIMIT
    assert config["recursion_limit"] < 50
