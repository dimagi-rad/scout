"""One agent run at a time per chat thread (arch review R08).

A live chat turn and the post-load resume both write the thread's LangGraph
checkpoint, which has no compare-and-set. The turn lease on the Thread row keeps
them from overlapping.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from procrastinate.exceptions import AlreadyEnqueued

from apps.chat import turn_lease
from apps.chat.models import Thread, ThreadJob
from apps.chat.turn_lease import aacquire_turn_lease, atry_acquire_turn_lease
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from apps.workspaces.tasks import (
    RESUME_BUSY_MAX_ATTEMPTS,
    RESUME_THREAD_BUSY_SUMMARY,
    _persist_synthetic_failure_message,
    resume_thread_after_materialization,
)

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

        with patch("apps.workspaces.tasks._build_agent_for_resume", build):
            await _persist_synthetic_failure_message(tj, "failed")

        build.assert_not_awaited()

    async def test_it_writes_and_frees_an_idle_thread(self):
        tj = await ThreadJob.objects.select_related("thread__workspace", "thread__user").aget(
            id=(await _resumable_job("synthetic-idle", 880008)).id
        )
        agent = MagicMock(aupdate_state=AsyncMock())

        with patch("apps.workspaces.tasks._build_agent_for_resume", AsyncMock(return_value=agent)):
            await _persist_synthetic_failure_message(tj, "failed")

        agent.aupdate_state.assert_awaited_once()
        assert (await _lease_row(tj.thread_id))["turn_lease_token"] is None


def test_lease_outlives_several_missed_heartbeats():
    assert (
        timedelta(seconds=turn_lease.TURN_LEASE_HEARTBEAT_SECONDS * 3) < turn_lease.TURN_LEASE_TTL
    )
