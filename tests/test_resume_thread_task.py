import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from langchain_core.messages import AIMessage

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.tasks import (
    RESUME_EXCEPTION_MESSAGE,
    RESUME_TIMEOUT_MESSAGE,
    resume_thread_after_materialization,
)

User = get_user_model()


async def _make_thread_job_ready_to_resume(
    *,
    email: str,
    ws_name: str,
    ext_id: str,
    schema_name: str,
    pj_id: int,
    tool_call: str,
    run_state=None,
):
    """Create a fully wired ThreadJob + MaterializationRun for resume tests."""
    if run_state is None:
        run_state = MaterializationRun.RunState.COMPLETED
    user = await User.objects.acreate_user(email=email, password="x")
    ws = await Workspace.objects.acreate(name=ws_name, created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id=ext_id,
        provider="commcare",
        canonical_name="Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name=schema_name,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=pj_id,
        tool_call_id=tool_call,
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=run_state,
        procrastinate_job_id=pj_id,
    )
    return user, ws, thread, tj


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_appends_system_message_and_invokes_agent():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t1", provider="commcare", canonical_name="Test Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_t1")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=3003,
        tool_call_id="tc3",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
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
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.COMPLETED
    # Inspect the input_state passed to ainvoke.
    call_args = mock_agent.ainvoke.await_args
    input_state = call_args.args[0]
    messages = input_state["messages"]
    assert len(messages) == 1
    assert messages[0].content.startswith("[__system_resume__]")
    assert "completed" in messages[0].content
    # Pin the runtime config's REAL, consumed contract: the checkpointer routes
    # the resume to the right conversation via configurable.thread_id. (We do not
    # assert on `oauth_tokens` — it is currently dead plumbing: build_agent_graph
    # accepts the kwarg and the resume forwards it into config, but nothing in the
    # graph reads it. Pinning it would codify dead plumbing as contract — 12#0
    # item 4.)
    config = call_args.args[1]
    assert config["configurable"]["thread_id"] == str(thread.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("run_state", "expected_status", "ext_id", "schema_name", "pj_id", "tool_call"),
    [
        (MaterializationRun.RunState.FAILED, "failed", "t-fail", "s_fail", 7071, "tc-fail"),
        (
            MaterializationRun.RunState.CANCELLED,
            "cancelled",
            "t-cancel",
            "s_cancel",
            7072,
            "tc-cancel",
        ),
    ],
)
async def test_resume_failed_or_cancelled_prompt_is_honest(
    run_state, expected_status, ext_id, schema_name, pj_id, tool_call
):
    """Finding 14#5: for a fully FAILED or CANCELLED materialization there is NO
    loaded data, so the resume prompt must NOT tell the agent the run "just
    completed ... using the now-loaded data". That framing invites the agent to
    query empty/absent schemas and claim a success that never happened. The
    prompt must instead say the materialization failed/was cancelled and that
    the agent should not claim success."""
    _user, _ws, _thread, tj = await _make_thread_job_ready_to_resume(
        email=f"{ext_id}@b.c",
        ws_name=f"W-{ext_id}",
        ext_id=ext_id,
        schema_name=schema_name,
        pj_id=pj_id,
        tool_call=tool_call,
        run_state=run_state,
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "resumed"
    body = mock_agent.ainvoke.await_args.args[0]["messages"][0].content
    lowered = body.lower()

    # The status must be reflected honestly.
    assert expected_status in lowered, f"prompt should mention {expected_status!r}: {body!r}"
    # The dishonest "just completed ... now-loaded data" framing must be gone.
    assert "now-loaded data" not in lowered, (
        f"{expected_status} prompt must NOT claim now-loaded data: {body!r}"
    )
    assert "just completed" not in lowered, (
        f"{expected_status} prompt must NOT claim the run just completed: {body!r}"
    )
    # And it must steer the agent away from claiming success.
    assert any(
        kw in lowered for kw in ("failed", "cancelled", "no data", "not claim", "did not")
    ), f"{expected_status} prompt must signal failure/absence of data: {body!r}"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_is_idempotent_when_already_claimed():
    """If the resume task is dispatched twice and the first claim wins, the
    second invocation no-ops."""
    user = await User.objects.acreate_user(email="b@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W2", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t2",
        provider="commcare",
        canonical_name="Test Tenant 2",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_t2")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=4040,
        tool_call_id="tc4",
        state=ThreadJob.State.COMPLETED,  # already terminal
    )
    await MaterializationRun.objects.acreate(
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
    user = await User.objects.acreate_user(email="c@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W3", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
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
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_partial_maps_to_failed():
    """A run still in LOADING state when resume fires yields 'partial' status,
    which should map to ThreadJob.State.FAILED."""
    user = await User.objects.acreate_user(email="d@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W4", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t4",
        provider="commcare",
        canonical_name="Test Tenant 4",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_t4")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=6060,
        tool_call_id="tc6",
        state=ThreadJob.State.PENDING,
    )
    # One run still in LOADING — triggers "partial" aggregate status.
    await MaterializationRun.objects.acreate(
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
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_invokes_agent_for_cancelled_threadjob():
    """Per Option A, a cancelled ThreadJob still triggers an agent response so
    the user gets a graceful follow-up message."""
    user = await User.objects.acreate_user(email="f@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-cancel-resume", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-cancel",
        provider="commcare",
        canonical_name="Cancel Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="s_cancel",
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=7777,
        tool_call_id="tc-cancel",
        state=ThreadJob.State.CANCELLED,  # Cancelled BEFORE resume runs
    )
    await MaterializationRun.objects.acreate(
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
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_bumps_thread_updated_at_on_success():
    """After a successful resume, Thread.updated_at is updated so the
    sidebar's green-dot indicator can fire (lastUpdated > lastViewed)."""

    user = await User.objects.acreate_user(email="e@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W5", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t5", provider="commcare", canonical_name="Test Tenant 5"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_t5")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=7070,
        tool_call_id="tc7",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
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
    await thread.arefresh_from_db()
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
    user = await User.objects.acreate_user(email="race@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-race", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-race",
        provider="commcare",
        canonical_name="Race Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_race")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=8484,
        tool_call_id="tc-race",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
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
    await tj.arefresh_from_db()
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
    user = await User.objects.acreate_user(email="stale@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-stale", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-stale",
        provider="commcare",
        canonical_name="Stale Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_stale")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=9595,
        tool_call_id="tc-stale",
        # ThreadJob got flipped to CANCELLED by a late Stop click,
        # but the runs already completed before the cancel landed.
        state=ThreadJob.State.CANCELLED,
    )
    await MaterializationRun.objects.acreate(
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
    user = await User.objects.acreate_user(email="psrc@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-psrc", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-psrc",
        provider="commcare_connect",
        canonical_name="Conn Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="s_psrc",
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=12345,
        tool_call_id="tc-psrc",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
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
    user = await User.objects.acreate_user(email="cas@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-cas", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-cas",
        provider="commcare",
        canonical_name="CAS Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_cas")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=24680,
        tool_call_id="tc-cas",
        state=ThreadJob.State.RUNNING,  # already-running; simulates a concurrent claim
    )
    await MaterializationRun.objects.acreate(
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
    user = await User.objects.acreate_user(email="rl@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-rl", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-rl", provider="commcare", canonical_name="RL Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_rl")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=9999,
        tool_call_id="tc-rl",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
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
        email="timeout@b.c",
        ws_name="W-timeout",
        ext_id="t-timeout",
        schema_name="s_timeout",
        pj_id=10001,
        tool_call="tc-timeout",
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
    await tj.arefresh_from_db()
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
        email="boom@b.c",
        ws_name="W-boom",
        ext_id="t-boom",
        schema_name="s_boom",
        pj_id=10002,
        tool_call="tc-boom",
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
    await tj.arefresh_from_db()
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
        email="bookend@b.c",
        ws_name="W-bookend",
        ext_id="t-bookend",
        schema_name="s_bookend",
        pj_id=10003,
        tool_call="tc-bookend",
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
        email="lf@b.c",
        ws_name="W-lf",
        ext_id="t-lf",
        schema_name="s_lf",
        pj_id=10004,
        tool_call="tc-lf",
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})

    span_cm = MagicMock()
    span_cm.__enter__ = MagicMock(return_value=MagicMock())
    span_cm.__exit__ = MagicMock(return_value=False)

    with (
        patch(
            "apps.workspaces.tasks._build_agent_for_resume",
            AsyncMock(return_value=(mock_agent, {})),
        ),
        patch(
            "apps.workspaces.tasks._resume_langfuse_span",
            return_value=span_cm,
        ) as span_helper,
    ):
        await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    span_helper.assert_called_once()
    kwargs = span_helper.call_args.kwargs
    assert kwargs["thread_job_id"] == str(tj.id)
    assert kwargs["thread_id"] == str(tj.thread_id)
    span_cm.__enter__.assert_called_once()
    span_cm.__exit__.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_agent_failure_sets_error_summary():
    """When agent.ainvoke raises, the ThreadJob is marked FAILED with a
    generic error_summary so the frontend can render an inline retry card."""
    user = await User.objects.acreate_user(email="afail@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-afail", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-afail",
        provider="commcare",
        canonical_name="AFail Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="s_afail",
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=4801,
        tool_call_id="tc-afail",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=4801,
    )

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("LLM 503"))
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    assert result["status"] == "agent_failed"
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert "agent failed to respond" in tj.error_summary.lower()
    assert "retry" in tj.error_summary.lower()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_partial_run_sets_threadjob_error_summary():
    """A PARTIAL aggregate maps to FAILED on the ThreadJob and the
    error_summary is composed from MaterializationRun.result["sources"]."""
    user = await User.objects.acreate_user(email="psum@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-psum", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-psum",
        provider="commcare_connect",
        canonical_name="PSum Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="s_psum",
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=4802,
        tool_call_id="tc-psum",
        state=ThreadJob.State.PENDING,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_connect",
        state=MaterializationRun.RunState.PARTIAL,
        procrastinate_job_id=4802,
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

    assert result["terminal_state"] == ThreadJob.State.FAILED
    await tj.arefresh_from_db()
    assert tj.error_summary
    # Names the failed source
    assert "completed_works" in tj.error_summary
    # Names the loaded sources + their row count
    assert "users" in tj.error_summary
    assert "98,969" in tj.error_summary  # users 100 + visits 98869
    # And calls out skipped
    assert "payments" in tj.error_summary


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_no_runs_sets_helpful_error_summary():
    """no_runs case: error_summary mentions credentials/pipelines so users
    can self-diagnose."""
    user = await User.objects.acreate_user(email="nr@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-nr", created_by=user)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=4803,
        tool_call_id="tc-nr",
        state=ThreadJob.State.PENDING,
    )
    # No MaterializationRun rows -> no_runs path.

    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
    with patch(
        "apps.workspaces.tasks._build_agent_for_resume",
        AsyncMock(return_value=(mock_agent, {})),
    ):
        await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.error_summary  # non-empty
    assert any(
        term in tj.error_summary.lower() for term in ("credentials", "pipeline", "pipelines")
    )


# ---------------------------------------------------------------------------
# Multi-tenant view-schema build failure surfacing (2026-06-10)
# ---------------------------------------------------------------------------


async def _make_multi_tenant_job(*, email, ws_name, pj_id, view_schema_state, last_error=""):
    """Wire a 2-tenant workspace whose per-tenant runs COMPLETED, with a
    WorkspaceViewSchema in the given state, ready to resume."""
    user = await User.objects.acreate_user(email=email, password="x")
    ws = await Workspace.objects.acreate(name=ws_name, created_by=user)
    for i in (1, 2):
        tenant = await Tenant.objects.acreate(
            external_id=f"{ws_name}-t{i}",
            provider="commcare",
            canonical_name=f"Tenant {i}",
        )
        await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
        schema = await TenantSchema.objects.acreate(
            tenant=tenant, schema_name=f"{ws_name}_s{i}".replace("-", "_")
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=schema,
            pipeline="commcare_sync",
            state=MaterializationRun.RunState.COMPLETED,
            procrastinate_job_id=pj_id,
            result={"rows": 100},
        )
    await WorkspaceViewSchema.objects.acreate(
        workspace=ws,
        schema_name=f"ws_{ws_name}".replace("-", "_")[:22],
        state=view_schema_state,
        last_error=last_error,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=pj_id,
        tool_call_id=f"tc-{ws_name}",
        state=ThreadJob.State.PENDING,
    )
    return tj


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_surfaces_view_schema_failure_for_multi_tenant():
    """All per-tenant runs COMPLETED but the WorkspaceViewSchema is FAILED:
    the resume prompt must explain the view-schema build failed, quote the
    error, and instruct the agent NOT to re-run materialization. The plain
    'just completed' success copy must NOT appear."""
    tj = await _make_multi_tenant_job(
        email="vsf@b.c",
        ws_name="W-vsf",
        pj_id=20001,
        view_schema_state=SchemaState.FAILED,
        last_error="Canonical name collision: 'a' and 'b' both sanitize to 'x'",
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
    lower = body.lower()
    # The view-schema-failure instruction must be present.
    assert "view schema" in lower
    assert "do not re-run materialization" in lower
    assert "Canonical name collision" in body  # the actual error text is quoted
    # The plain success copy must NOT appear.
    assert "Materialization just completed" not in body
    # The job lands in FAILED with a system-fix error summary.
    assert result["terminal_state"] == ThreadJob.State.FAILED
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert "view schema" in tj.error_summary.lower()
    assert "Canonical name collision" in tj.error_summary


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_plain_completed_for_multi_tenant_active_view_schema():
    """Regression: when the WorkspaceViewSchema is ACTIVE, the multi-tenant
    resume uses the normal 'just completed' copy, not the failure branch."""
    tj = await _make_multi_tenant_job(
        email="vsa@b.c",
        ws_name="W-vsa",
        pj_id=20002,
        view_schema_state=SchemaState.ACTIVE,
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
    assert "Materialization just completed" in body
    assert "view schema" not in body.lower()
    assert result["terminal_state"] == ThreadJob.State.COMPLETED
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.COMPLETED
