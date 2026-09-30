"""One agent run at a time per chat thread.

The LangGraph checkpointer has no compare-and-set: two runs on one thread each
write checkpoints parented on their own last step, so whichever writes last
wins and the other's messages drop out of the conversation. A live chat turn
and a background resume (``resume_thread_after_materialization``) must
therefore not run on the same thread at once (arch review R08).

The lease is a token plus an expiry on the Thread row, taken with a single
conditional UPDATE and renewed by a heartbeat while the run is alive. A holder
that dies without releasing stops heartbeating, so its lease lapses after
``TURN_LEASE_TTL`` instead of locking the thread for good. Expiry is judged by
the database clock so web and worker hosts agree on it.

A live holder whose heartbeat cannot keep the lease (the DB refused renewals
for a whole TTL, or another run took the thread after a stall) is cancelled
rather than left writing a thread it no longer owns.

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
    """A held lease on one thread."""

    def __init__(self, thread_id, token: uuid.UUID):
        self.thread_id = thread_id
        self.token = token
        self.lost = False
        self._released = False

    async def renew(self) -> bool:
        return bool(
            await Thread.objects.filter(id=self.thread_id, turn_lease_token=self.token).aupdate(
                turn_lease_expires_at=Now() + TURN_LEASE_TTL
            )
        )

    async def release(self) -> None:
        """Clear the lease if this holder still has it; retried on the next call if it fails."""
        if self._released:
            return
        # Shielded: the stream releases from a cancelled task on client disconnect.
        await asyncio.shield(
            Thread.objects.filter(id=self.thread_id, turn_lease_token=self.token).aupdate(
                turn_lease_token=None, turn_lease_expires_at=None
            )
        )
        self._released = True

    def release_sync(self) -> None:
        """``release`` for sync callers, such as Django closing a response."""
        if self._released:
            return
        Thread.objects.filter(id=self.thread_id, turn_lease_token=self.token).update(
            turn_lease_token=None, turn_lease_expires_at=None
        )
        self._released = True

    @contextlib.asynccontextmanager
    async def kept_alive(self) -> AsyncIterator[TurnLease]:
        """Heartbeat the lease for the body's duration, cancelling the body if it is lost."""
        heartbeat = asyncio.create_task(self._heartbeat(asyncio.current_task()))
        try:
            yield self
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    @contextlib.asynccontextmanager
    async def held(self) -> AsyncIterator[TurnLease]:
        """``kept_alive``, then release the lease."""
        try:
            async with self.kept_alive():
                yield self
        finally:
            await self.release()

    async def _heartbeat(self, owner: asyncio.Task | None) -> None:
        loop = asyncio.get_running_loop()
        last_renewed = loop.time()
        while True:
            await asyncio.sleep(TURN_LEASE_HEARTBEAT_SECONDS)
            try:
                renewed = await self.renew()
            except Exception:
                logger.warning("turn lease: renew failed for thread %s", self.thread_id)
                if loop.time() - last_renewed < TURN_LEASE_TTL.total_seconds():
                    continue
                renewed = False
            if renewed:
                last_renewed = loop.time()
                continue
            logger.error(
                "turn lease: lost the lease on thread %s mid-run; cancelling the run",
                self.thread_id,
            )
            self.lost = True
            if owner is not None:
                owner.cancel()
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
