"""One agent run at a time per chat thread (arch review R08).

A live chat turn and the post-load resume both write the thread's LangGraph
checkpoint, which has no compare-and-set. The turn lease on the Thread row keeps
them from overlapping.
"""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.chat import turn_lease
from apps.chat.models import Thread
from apps.chat.turn_lease import aacquire_turn_lease, atry_acquire_turn_lease
from apps.workspaces.models import (
    Workspace,
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


def test_lease_outlives_several_missed_heartbeats():
    assert (
        timedelta(seconds=turn_lease.TURN_LEASE_HEARTBEAT_SECONDS * 3) < turn_lease.TURN_LEASE_TTL
    )
