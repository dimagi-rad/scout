"""One agent run at a time per chat thread (arch review R08).

A live chat turn and the post-load resume both write the thread's LangGraph
checkpoint, which has no compare-and-set. The turn lease on the Thread row keeps
them from overlapping.
"""

import asyncio
import contextlib
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import AsyncClient, override_settings
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.exceptions import AlreadyEnqueued

from apps.chat import turn_lease
from apps.chat.models import Thread, ThreadJob
from apps.chat.stream import langgraph_to_ui_stream
from apps.chat.turn_lease import aacquire_turn_lease, atry_acquire_turn_lease
from apps.chat.views import _TurnStreamingResponse
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.reconciliation import (
    RESUME_TASK_NAME,
    _resume_in_flight,
    persist_synthetic_failure_message,
    reconcile_stale_thread_job,
)
from apps.workspaces.tasks import (
    RESUME_BUSY_MAX_ATTEMPTS,
    RESUME_THREAD_BUSY_SUMMARY,
    resume_thread_after_materialization,
)
from tests.tenant_access import ausable_connection

User = get_user_model()


async def _thread(slug: str) -> Thread:
    user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
    return await Thread.objects.acreate(workspace=ws, user=user)


async def _lease_row(thread_id):
    return (
        await Thread.objects.filter(id=thread_id)
        .values("turn_lease_token", "turn_lease_expires_at")
        .aget()
    )


def _fail_unawaited(coro):
    coro.close()
    raise RuntimeError("db down")


async def _expire(thread_id):
    await Thread.objects.filter(id=thread_id).aupdate(
        turn_lease_expires_at=timezone.now() - timedelta(seconds=1)
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestLease:
    async def test_a_held_lease_refuses_a_second_holder_until_released(self):
        thread = await _thread("lease-basic")
        first = await atry_acquire_turn_lease(thread.id)
        assert first is not None
        assert await atry_acquire_turn_lease(thread.id) is None

        await first.release()

        assert await _lease_row(thread.id) == {
            "turn_lease_token": None,
            "turn_lease_expires_at": None,
        }
        assert await atry_acquire_turn_lease(thread.id) is not None

    async def test_a_crashed_holders_lease_lapses_and_cannot_be_reclaimed_by_it(self):
        thread = await _thread("lease-stale")
        crashed = await atry_acquire_turn_lease(thread.id)
        await _expire(thread.id)

        successor = await atry_acquire_turn_lease(thread.id)

        assert successor is not None
        assert not await crashed.renew()
        await crashed.release()
        assert (await _lease_row(thread.id))["turn_lease_token"] == successor.token

    async def test_held_heartbeats_and_releases_even_when_the_body_raises(self):
        thread = await _thread("lease-held")
        lease = await atry_acquire_turn_lease(thread.id)
        await Thread.objects.filter(id=thread.id).aupdate(
            turn_lease_expires_at=timezone.now() + timedelta(seconds=1)
        )
        with (
            patch.object(turn_lease, "TURN_LEASE_HEARTBEAT_SECONDS", 0.05),
            pytest.raises(RuntimeError),
        ):
            async with lease.held():
                await asyncio.sleep(0.2)
                expires = (await _lease_row(thread.id))["turn_lease_expires_at"]
                assert expires > timezone.now() + timedelta(seconds=30)
                raise RuntimeError("turn failed")

        assert (await _lease_row(thread.id))["turn_lease_token"] is None

    async def test_a_run_that_loses_its_lease_is_cancelled(self):
        thread = await _thread("lease-lost")
        lease = await atry_acquire_turn_lease(thread.id)
        wrote_after_loss = []

        async def run():
            async with lease.kept_alive():
                await _expire(thread.id)
                await atry_acquire_turn_lease(thread.id)
                await asyncio.sleep(5)
                wrote_after_loss.append(True)

        with patch.object(turn_lease, "TURN_LEASE_HEARTBEAT_SECONDS", 0.05):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(run())

        assert lease.lost
        assert wrote_after_loss == []

    async def test_a_run_whose_renewals_keep_failing_is_cancelled_after_a_ttl(self):
        thread = await _thread("lease-renew-fails")
        lease = await atry_acquire_turn_lease(thread.id)

        async def run():
            async with lease.kept_alive():
                await asyncio.sleep(5)

        with (
            patch.object(turn_lease, "TURN_LEASE_HEARTBEAT_SECONDS", 0.05),
            patch.object(turn_lease, "TURN_LEASE_TTL", timedelta(seconds=0.2)),
            patch.object(lease, "renew", AsyncMock(side_effect=RuntimeError("db down"))),
            pytest.raises(asyncio.CancelledError),
        ):
            await asyncio.create_task(run())

        assert lease.lost

    async def test_a_failed_release_can_be_retried(self):
        thread = await _thread("lease-release-retry")
        lease = await atry_acquire_turn_lease(thread.id)

        with (
            patch.object(turn_lease.asyncio, "shield", side_effect=_fail_unawaited),
            pytest.raises(RuntimeError),
        ):
            await lease.release()
        await lease.release()

        assert (await _lease_row(thread.id))["turn_lease_token"] is None

    async def test_acquire_waits_for_a_release(self):
        thread = await _thread("lease-wait")
        holder = await atry_acquire_turn_lease(thread.id)

        async def release_soon():
            await asyncio.sleep(0.1)
            await holder.release()

        releaser = asyncio.create_task(release_soon())
        lease = await aacquire_turn_lease(thread.id, wait_seconds=2, poll_seconds=0.05)
        await releaser

        assert lease is not None


async def _chat_member(slug: str):
    user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name="Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.READ)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    client = AsyncClient()
    await client.alogin(email=f"{slug}@b.c", password="x")
    return ws, thread, client


async def _post_chat(client, ws, thread):
    return await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [{"role": "user", "content": "follow-up"}],
                "workspaceId": str(ws.id),
                "threadId": str(thread.id),
            }
        ),
        content_type="application/json",
    )


@contextlib.contextmanager
def _agent_layer(stream=None, build_agent=None):
    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock),
        patch("apps.chat.views.build_agent_graph", build_agent or AsyncMock()),
        patch(
            "apps.chat.views.repair_dangling_tool_calls", new_callable=AsyncMock, return_value=[]
        ),
        patch("apps.chat.views.langgraph_to_ui_stream", side_effect=stream or _one_chunk),
        patch("apps.chat.views.TURN_LEASE_WAIT_SECONDS", 0),
    ):
        yield


async def _one_chunk(*_args, **_kwargs):
    yield "data: {}\n\n"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestChatTurn:
    async def test_a_turn_is_refused_while_another_run_holds_the_thread(self):
        ws, thread, client = await _chat_member("chat-busy")
        await atry_acquire_turn_lease(thread.id)
        build_agent = AsyncMock()

        with _agent_layer(build_agent=build_agent):
            resp = await _post_chat(client, ws, thread)

        assert resp.status_code == 409
        # "busy" is what the chat UI's resend path keys on (frontend/src/api/busy.ts).
        assert resp.json()["error"] == "busy"
        assert resp.json()["reason"] == "thread_busy"
        assert resp["Retry-After"]
        build_agent.assert_not_awaited()

    async def test_the_stream_holds_the_thread_until_it_finishes(self):
        ws, thread, client = await _chat_member("chat-holds")
        seen_during_stream = []

        async def fake_stream(*_args, **_kwargs):
            seen_during_stream.append(await atry_acquire_turn_lease(thread.id))
            yield "data: {}\n\n"

        with _agent_layer(fake_stream):
            resp = await _post_chat(client, ws, thread)
            assert resp.status_code == 200
            _ = [chunk async for chunk in resp.streaming_content]

        assert seen_during_stream == [None]
        assert (await _lease_row(thread.id))["turn_lease_token"] is None

    async def test_a_turn_that_fails_to_start_releases_the_thread(self):
        ws, thread, client = await _chat_member("chat-fail")
        build_agent = AsyncMock(side_effect=RuntimeError("no agent"))

        with _agent_layer(build_agent=build_agent):
            resp = await _post_chat(client, ws, thread)

        assert resp.status_code == 500
        assert (await _lease_row(thread.id))["turn_lease_token"] is None

    async def test_a_turn_that_loses_the_thread_while_starting_is_told_it_is_busy(self):
        ws, thread, client = await _chat_member("chat-lost")

        async def lose_the_thread_then_stall(*_args, **_kwargs):
            await _expire(thread.id)
            await atry_acquire_turn_lease(thread.id)
            await asyncio.sleep(5)

        with (
            _agent_layer(build_agent=AsyncMock(side_effect=lose_the_thread_then_stall)),
            patch.object(turn_lease, "TURN_LEASE_HEARTBEAT_SECONDS", 0.05),
        ):
            resp = await _post_chat(client, ws, thread)

        assert resp.status_code == 409
        assert resp.json()["reason"] == "thread_busy"

    async def test_a_lapsed_lease_does_not_block_the_user(self):
        ws, thread, client = await _chat_member("chat-stale")
        await atry_acquire_turn_lease(thread.id)
        await _expire(thread.id)

        with _agent_layer():
            resp = await _post_chat(client, ws, thread)
            assert resp.status_code == 200
            _ = [chunk async for chunk in resp.streaming_content]


async def _resumable_job(slug: str, pj_id: int) -> ThreadJob:
    user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name="Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name=f"s_{pj_id}")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=pj_id,
    )
    return await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=pj_id,
        tool_call_id=f"tc-{slug}",
        state=ThreadJob.State.PENDING,
    )


def _requeue_capture(side_effect=None):
    configured = MagicMock()
    configured.defer_async = AsyncMock(side_effect=side_effect)
    return (
        patch.object(
            resume_thread_after_materialization, "configure", MagicMock(return_value=configured)
        ),
        configured,
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestResume:
    async def test_a_live_turn_defers_the_resume_without_touching_the_thread(self):
        tj = await _resumable_job("resume-busy", 880001)
        live = await atry_acquire_turn_lease(tj.thread_id)
        agent = MagicMock(ainvoke=AsyncMock())
        requeue, configured = _requeue_capture()

        with (
            requeue,
            patch("apps.workspaces.tasks._build_agent_for_resume", AsyncMock(return_value=agent)),
        ):
            result = await resume_thread_after_materialization(
                None, thread_job_id=str(tj.id), busy_attempt=2
            )

        assert result == {"status": "thread_busy_deferred", "retry_in_seconds": 20}
        agent.ainvoke.assert_not_awaited()
        configured.defer_async.assert_awaited_once_with(thread_job_id=str(tj.id), busy_attempt=3)
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.PENDING
        assert (await _lease_row(tj.thread_id))["turn_lease_token"] == live.token

    async def test_a_resume_already_queued_for_the_busy_thread_is_not_doubled(self):
        tj = await _resumable_job("resume-dup", 880002)
        await atry_acquire_turn_lease(tj.thread_id)
        requeue, _ = _requeue_capture(side_effect=AlreadyEnqueued("queued"))

        with requeue:
            result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

        assert result == {"status": "thread_busy_already_queued"}

    async def test_a_thread_that_stays_busy_fails_the_job_instead_of_waiting_forever(self):
        tj = await _resumable_job("resume-giveup", 880003)
        await atry_acquire_turn_lease(tj.thread_id)
        requeue, configured = _requeue_capture()
        persist = AsyncMock()

        with requeue, patch("apps.workspaces.tasks._persist_synthetic_failure_message", persist):
            result = await resume_thread_after_materialization(
                None, thread_job_id=str(tj.id), busy_attempt=RESUME_BUSY_MAX_ATTEMPTS
            )

        assert result == {"status": "thread_busy_gave_up"}
        configured.defer_async.assert_not_awaited()
        persist.assert_not_awaited()
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.FAILED
        assert tj.failure_phase == ThreadJob.FailurePhase.RESUME
        assert tj.error_summary == RESUME_THREAD_BUSY_SUMMARY

    async def test_giving_up_keeps_a_users_cancel(self):
        tj = await _resumable_job("resume-giveup-cancel", 880009)
        await ThreadJob.objects.filter(id=tj.id).aupdate(state=ThreadJob.State.CANCELLED)
        await atry_acquire_turn_lease(tj.thread_id)
        requeue, _ = _requeue_capture()

        with requeue:
            await resume_thread_after_materialization(
                None, thread_job_id=str(tj.id), busy_attempt=RESUME_BUSY_MAX_ATTEMPTS
            )

        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.CANCELLED
        assert tj.error_summary == ""

    async def test_an_idle_thread_resumes_under_the_lease_and_frees_it(self):
        tj = await _resumable_job("resume-idle", 880004)
        seen_during_invoke = []

        async def ainvoke(*_args, **_kwargs):
            seen_during_invoke.append(await atry_acquire_turn_lease(tj.thread_id))
            return {"messages": []}

        agent = MagicMock(ainvoke=AsyncMock(side_effect=ainvoke))
        with patch("apps.workspaces.tasks._build_agent_for_resume", AsyncMock(return_value=agent)):
            result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

        assert result["status"] == "resumed"
        assert seen_during_invoke == [None]
        assert (await _lease_row(tj.thread_id))["turn_lease_token"] is None

    async def test_a_lapsed_lease_from_a_crashed_turn_does_not_block_the_resume(self):
        tj = await _resumable_job("resume-stale", 880005)
        await atry_acquire_turn_lease(tj.thread_id)
        await _expire(tj.thread_id)
        agent = MagicMock(ainvoke=AsyncMock(return_value={"messages": []}))

        with patch("apps.workspaces.tasks._build_agent_for_resume", AsyncMock(return_value=agent)):
            result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

        assert result["status"] == "resumed"
        agent.ainvoke.assert_awaited_once()

    async def test_a_resume_that_loses_the_claim_frees_the_thread(self):
        tj = await _resumable_job("resume-claimed", 880006)
        await ThreadJob.objects.filter(id=tj.id).aupdate(state=ThreadJob.State.RUNNING)

        result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

        assert result == {"status": "already_claimed"}
        assert (await _lease_row(tj.thread_id))["turn_lease_token"] is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestSyntheticFailureMessage:
    async def test_it_is_skipped_while_a_live_turn_holds_the_thread(self):
        tj = await ThreadJob.objects.select_related("thread__workspace", "thread__user").aget(
            id=(await _resumable_job("synthetic-busy", 880007)).id
        )
        await atry_acquire_turn_lease(tj.thread_id)
        build = AsyncMock()

        with patch("apps.workspaces.services.reconciliation.build_agent_for_resume", build):
            await persist_synthetic_failure_message(tj, "failed")

        build.assert_not_awaited()

    async def test_it_writes_and_frees_an_idle_thread(self):
        tj = await ThreadJob.objects.select_related("thread__workspace", "thread__user").aget(
            id=(await _resumable_job("synthetic-idle", 880008)).id
        )
        agent = MagicMock(aupdate_state=AsyncMock())

        with patch(
            "apps.workspaces.services.reconciliation.build_agent_for_resume",
            AsyncMock(return_value=agent),
        ):
            await persist_synthetic_failure_message(tj, "failed")

        agent.aupdate_state.assert_awaited_once()
        assert (await _lease_row(tj.thread_id))["turn_lease_token"] is None


def test_lease_outlives_several_missed_heartbeats():
    assert (
        timedelta(seconds=turn_lease.TURN_LEASE_HEARTBEAT_SECONDS * 3) < turn_lease.TURN_LEASE_TTL
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestUnsentResponse:
    async def test_closing_an_unsent_response_frees_the_thread(self):
        thread = await _thread("unsent-close")
        response = _TurnStreamingResponse(await atry_acquire_turn_lease(thread.id))

        await sync_to_async(response.close)()

        assert (await _lease_row(thread.id))["turn_lease_token"] is None

    async def test_closing_a_started_response_leaves_release_to_the_stream(self):
        thread = await _thread("started-close")
        lease = await atry_acquire_turn_lease(thread.id)
        response = _TurnStreamingResponse(lease)
        response.turn_started = True

        await sync_to_async(response.close)()

        assert (await _lease_row(thread.id))["turn_lease_token"] == lease.token


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestResumeDeadline:
    async def test_a_resume_hung_outside_the_ainvoke_timeout_fails_and_frees_the_thread(self):
        tj = await _resumable_job("resume-hung", 880010)

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(30)

        persist = AsyncMock()
        with (
            override_settings(AGENT_RESUME_TIMEOUT_S=0),
            patch("apps.workspaces.tasks.RESUME_SETUP_BUDGET_SECONDS", 0.2),
            patch("apps.workspaces.tasks._build_agent_for_resume", side_effect=hang),
            patch("apps.workspaces.tasks._persist_synthetic_failure_message", persist),
        ):
            result = await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

        assert result == {"status": "resume_deadline"}
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.FAILED
        assert tj.failure_phase == ThreadJob.FailurePhase.RESUME
        persist.assert_awaited_once()
        assert persist.await_args.kwargs == {"holds_turn_lease": True}
        assert (await _lease_row(tj.thread_id))["turn_lease_token"] is None


@pytest.fixture
def queued_jobs():
    """procrastinate_jobs is unmanaged, so the transactional flush leaves rows behind."""
    before = set(ProcrastinateJob.objects.values_list("id", flat=True))
    yield
    added = set(ProcrastinateJob.objects.values_list("id", flat=True)) - before
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [list(added)])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestReconcilerDuringBackoff:
    async def test_a_queued_resume_is_seen_as_in_flight(self, queued_jobs):
        tj = await _resumable_job("reconcile-backoff", 880011)
        assert not await _resume_in_flight(tj.id)

        await resume_thread_after_materialization.configure(
            schedule_in={"seconds": 60}
        ).defer_async(thread_job_id=str(tj.id), busy_attempt=3)

        assert await _resume_in_flight(tj.id)

    async def test_a_resume_whose_worker_died_is_not_in_flight(self, queued_jobs):
        tj = await _resumable_job("reconcile-dead-worker", 880013)
        job_id = await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))

        with patch(
            "apps.workspaces.services.reconciliation._stalled_procrastinate_job_ids",
            return_value={job_id},
        ):
            assert not await _resume_in_flight(tj.id)

    async def test_the_reconciler_does_not_restart_a_backing_off_resume(self):
        tj = await _resumable_job("reconcile-skip", 880012)
        reconciliation = "apps.workspaces.services.reconciliation"

        with (
            patch(f"{reconciliation}._procrastinate_job_status", return_value="succeeded"),
            patch(f"{reconciliation}._resume_in_flight", return_value=True),
            patch(f"{reconciliation}.app.configure_task") as configure_resume,
        ):
            action = await reconcile_stale_thread_job(tj)

        assert action is None
        configure_resume.assert_not_called()


def test_the_in_flight_lookup_names_the_real_task():
    assert resume_thread_after_materialization.name == RESUME_TASK_NAME


@pytest.mark.asyncio
@pytest.mark.parametrize(("owns", "persisted"), [(True, 1), (False, 0)])
async def test_a_cancelled_stream_saves_its_partial_reply_only_while_it_owns_the_thread(
    owns, persisted
):
    async def events(*_args, **_kwargs):
        await asyncio.sleep(30)
        yield {}

    agent = MagicMock(astream_events=events, aupdate_state=AsyncMock())

    async def consume():
        async for _chunk in langgraph_to_ui_stream(
            agent, {}, {"configurable": {}}, owns_thread=lambda: owns
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert agent.aupdate_state.await_count == persisted
