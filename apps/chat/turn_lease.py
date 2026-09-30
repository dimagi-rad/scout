"""One agent run at a time per chat thread.

The LangGraph checkpointer has no compare-and-set: two runs on one thread each
write checkpoints parented on their own last step, so whichever writes last
wins and the other's messages drop out of the conversation. A live chat turn
and a background resume (``resume_thread_after_materialization``) must
therefore never run on the same thread at once (arch review R08).

The lease is a token plus an expiry on the Thread row, taken with a single
conditional UPDATE and renewed by a heartbeat while the run is alive. A holder
that dies without releasing stops heartbeating, so its lease lapses after
``TURN_LEASE_TTL`` instead of locking the thread for good. Expiry is judged by
the database clock so web and worker hosts agree on it.

A session advisory lock would release on a crash too, but it pins a platform DB
connection for the whole stream, and connections are the scarce resource here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

from django.db.models import Q
from django.db.models.functions import Now

from apps.chat.models import Thread

logger = logging.getLogger(__name__)

TURN_LEASE_TTL = timedelta(seconds=90)
TURN_LEASE_HEARTBEAT_SECONDS = 20


class TurnLease:
    """A held lease on one thread. ``release`` is idempotent."""

    def __init__(self, thread_id, token: uuid.UUID):
        self.thread_id = thread_id
        self.token = token
        self._released = False

    async def renew(self) -> bool:
        return bool(
            await Thread.objects.filter(id=self.thread_id, turn_lease_token=self.token).aupdate(
                turn_lease_expires_at=Now() + TURN_LEASE_TTL
            )
        )

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        # Shielded: the stream releases from a cancelled task on client disconnect.
        await asyncio.shield(
            Thread.objects.filter(id=self.thread_id, turn_lease_token=self.token).aupdate(
                turn_lease_token=None, turn_lease_expires_at=None
            )
        )

    @contextlib.asynccontextmanager
    async def held(self) -> AsyncIterator[TurnLease]:
        """Heartbeat the lease for the body's duration, then release it."""
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            yield self
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self.release()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(TURN_LEASE_HEARTBEAT_SECONDS)
            try:
                renewed = await self.renew()
            except Exception:
                logger.warning("turn lease: renew failed for thread %s", self.thread_id)
                continue
            if not renewed:
                # Only reachable after a stall longer than the TTL let another run
                # take the thread; nothing here can undo that, so say so and stop.
                logger.error("turn lease: lost lease on thread %s mid-run", self.thread_id)
                return


async def atry_acquire_turn_lease(thread_id) -> TurnLease | None:
    """Take the thread's lease if nobody holds a live one, else return None."""
    token = uuid.uuid4()
    acquired = (
        await Thread.objects.filter(id=thread_id)
        .filter(Q(turn_lease_expires_at__isnull=True) | Q(turn_lease_expires_at__lt=Now()))
        .aupdate(turn_lease_token=token, turn_lease_expires_at=Now() + TURN_LEASE_TTL)
    )
    return TurnLease(thread_id, token) if acquired else None


async def aacquire_turn_lease(
    thread_id, *, wait_seconds: float = 0, poll_seconds: float = 0.25
) -> TurnLease | None:
    """``atry_acquire_turn_lease``, retried for up to ``wait_seconds``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        lease = await atry_acquire_turn_lease(thread_id)
        if lease is not None or loop.time() >= deadline:
            return lease
        await asyncio.sleep(poll_seconds)
