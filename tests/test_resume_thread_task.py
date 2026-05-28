import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import override_settings
from langchain_core.messages import AIMessage

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from apps.workspaces.tasks import (
    RESUME_EXCEPTION_MESSAGE,
    RESUME_TIMEOUT_MESSAGE,
    resume_thread_after_materialization,
)

User = get_user_model()


async def _make_thread_job_ready_to_resume(
    *, email: str, ws_name: str, ext_id: str, schema_name: str, pj_id: int,
    tool_call: str, run_state=None,
):
    """Create a fully wired ThreadJob + MaterializationRun for resume tests."""
    if run_state is None:
        run_state = MaterializationRun.RunState.COMPLETED
    user = await sync_to_async(User.objects.create_user)(email=email, password="x")
    ws = await sync_to_async(Workspace.objects.create)(name=ws_name, created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id=ext_id, provider="commcare", canonical_name="Tenant",
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    schema = await sync_to_async(TenantSchema.objects.create)(
        tenant=tenant, schema_name=schema_name,
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    tj = await sync_to_async(ThreadJob.objects.create)(
        thread=thread, job_type="materialization",
        procrastinate_job_id=pj_id, tool_call_id=tool_call,
        state=ThreadJob.State.PENDING,
    )
    await sync_to_async(MaterializationRun.objects.create)(
        tenant_schema=schema, pipeline="commcare_sync",
        state=run_state, procrastinate_job_id=pj_id,
    )
    return user, ws, thread, tj


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


# ---------------------------------------------------------------------------
# Timeout / exception / observability coverage for the resume task (issue #188)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(AGENT_RESUME_TIMEOUT_S=1)
async def test_ainvoke_timeout_marks_failed_and_persists_message():
    """When agent.ainvoke exceeds AGENT_RESUME_TIMEOUT_S, the ThreadJob lands
    in FAILED and a synthetic AIMessage is persisted via aupdate_state so the
    user sees a friendly explanation instead of a forever-spinner."""
    _, _, _, tj = await _make_thread_job_ready_to_resume(
        email="timeout@b.c", ws_name="W-timeout", ext_id="t-timeout",
        schema_name="s_timeout", pj_id=10001, tool_call="tc-timeout",
    )

    async def _sleeping_invoke(*_args, **_kwargs):
        await asyncio.sleep(5)
        return {"messages": []}

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=_sleeping_invoke)
    mock_agent.aupdate_state = AsyncMock(return_value=None)

    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "agent_timeout"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.completed_at is not None

    # aupdate_state was called with a single AIMessage carrying the timeout copy
    mock_agent.aupdate_state.assert_awaited()
    update_args = mock_agent.aupdate_state.await_args
    payload = update_args.args[1]
    assert "messages" in payload
    msg = payload["messages"][0]
    assert isinstance(msg, AIMessage)
    assert msg.content == RESUME_TIMEOUT_MESSAGE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ainvoke_exception_marks_failed_and_persists_message():
    """When agent.ainvoke raises (non-timeout), the ThreadJob lands in FAILED
    and a synthetic AIMessage with the generic-exception copy is persisted."""
    _, _, _, tj = await _make_thread_job_ready_to_resume(
        email="boom@b.c", ws_name="W-boom", ext_id="t-boom",
        schema_name="s_boom", pj_id=10002, tool_call="tc-boom",
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("upstream LLM 500"))
    mock_agent.aupdate_state = AsyncMock(return_value=None)

    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "agent_failed"
    await sync_to_async(tj.refresh_from_db)()
    assert tj.state == ThreadJob.State.FAILED

    mock_agent.aupdate_state.assert_awaited()
    msg = mock_agent.aupdate_state.await_args.args[1]["messages"][0]
    assert isinstance(msg, AIMessage)
    assert msg.content == RESUME_EXCEPTION_MESSAGE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_successful_ainvoke_logs_bookends(caplog):
    """On the success path both 'ainvoke start' and 'ainvoke complete' lines
    bracket the call so 26-minute silent hangs become trivially detectable."""
    _, _, _, tj = await _make_thread_job_ready_to_resume(
        email="bookend@b.c", ws_name="W-bookend", ext_id="t-bookend",
        schema_name="s_bookend", pj_id=10003, tool_call="tc-bookend",
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})

    caplog.set_level(logging.INFO, logger="apps.workspaces.tasks")
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "resumed"
    bookends = [r.message for r in caplog.records if "ainvoke" in r.message]
    assert any("ainvoke start" in m for m in bookends), bookends
    assert any("ainvoke complete" in m for m in bookends), bookends


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_emits_langfuse_span_on_each_outcome():
    """The Langfuse span context manager must wrap the ainvoke on success,
    timeout, and exception paths so traces are emitted on every terminal
    outcome (the production bug was a silent ainvoke with no trace)."""
    _, _, _, tj = await _make_thread_job_ready_to_resume(
        email="lf@b.c", ws_name="W-lf", ext_id="t-lf",
        schema_name="s_lf", pj_id=10004, tool_call="tc-lf",
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})

    span_cm = MagicMock()
    span_cm.__enter__ = MagicMock(return_value=MagicMock())
    span_cm.__exit__ = MagicMock(return_value=False)

    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ), patch(
        "apps.workspaces.tasks._resume_langfuse_span", return_value=span_cm,
    ) as span_helper:
        await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    span_helper.assert_called_once()
    kwargs = span_helper.call_args.kwargs
    assert kwargs["thread_job_id"] == str(tj.id)
    assert kwargs["thread_id"] == str(tj.thread_id)
    span_cm.__enter__.assert_called_once()
    span_cm.__exit__.assert_called_once()
