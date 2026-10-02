"""Held requests: what a user types while their chat's first data load runs.

Everything typed during the load is one unsent turn, kept here instead of the
checkpoint so no LLM turn runs before the data can answer it. The load's resume,
or the next live turn on the thread, claims it under the thread's turn lease and
sends it as one HumanMessage whose id is derived from the request's version; the
row is deleted once that id is in the checkpoint and reverted to waiting otherwise.

A claim is live only while its token is the thread's unexpired lease token. Only a
lease holder takes over a stale claim, because only it can read the checkpoint
without racing the writer: a stale claim whose message already landed is dropped
rather than sent twice. Every change takes the row lock, so an add that loses the
race with a claim is refused (409) instead of being silently left out.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.constants import MAX_MESSAGE_LENGTH
from apps.chat.models import PendingRequest, Thread, ThreadJob

logger = logging.getLogger(__name__)

PART_SEPARATOR = "\n\n"
# AI SDK message ids are short; a longer client id is not one.
MAX_PART_ID_LENGTH = 128
# Each part is stored, locked and polled whole; the text cap alone allows thousands.
MAX_PARTS = 50
REQUEST_TOO_LONG_MESSAGE = "Request too long — edit it"
TOO_MANY_PARTS_MESSAGE = "That's as much as one request can hold — wait for your data"
SETTLE_TIMEOUT_SECONDS = 15


class PendingRequestConflict(Exception):
    """The request was claimed, changed or settled since the caller last saw it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PendingRequestTooLong(Exception):
    pass


@dataclass(frozen=True)
class ClaimedRequest:
    thread_id: str
    request_id: uuid.UUID
    version: int
    text: str
    token: uuid.UUID

    @property
    def message_id(self) -> str:
        return held_message_id(self.request_id, self.version)

    @property
    def marker_id(self) -> str:
        return f"{self.message_id}-sys"


@dataclass(frozen=True)
class _StaleClaim:
    request_id: uuid.UUID
    version: int
    token: uuid.UUID


def held_message_id(request_id, version: int) -> str:
    # Per request, not per thread: a thread holds again after a send, and an id
    # already in the checkpoint would replace that message rather than append.
    return f"pr-{request_id}-{version}"


def combined_text(parts: list[dict]) -> str:
    return PART_SEPARATOR.join(part["text"] for part in parts)


def _check_length(text: str, part_count: int = 1) -> None:
    if len(text) > MAX_MESSAGE_LENGTH:
        raise PendingRequestTooLong(REQUEST_TOO_LONG_MESSAGE)
    if part_count > MAX_PARTS:
        raise PendingRequestTooLong(TOO_MANY_PARTS_MESSAGE)


def _new_part(part_id: str, text: str) -> dict:
    return {"id": part_id, "text": text, "added_at": timezone.now().isoformat()}


def _claim_is_live(pending: PendingRequest, thread: Thread) -> bool:
    return (
        pending.state == PendingRequest.State.CLAIMED
        and pending.claim_token is not None
        and thread.turn_lease_token == pending.claim_token
        and thread.turn_lease_expires_at is not None
        and thread.turn_lease_expires_at > timezone.now()
    )


def serialize(pending: PendingRequest, thread: Thread, job_state: str | None) -> dict:
    """The request as the chat shows it; a stale claim shows as waiting again.

    ``job_state`` is its load's ThreadJob state: a waiting request whose load is no
    longer pending or running will not be sent unless the user sends it.
    """
    return {
        "thread_id": str(pending.thread_id),
        "request_id": str(pending.request_id),
        "version": pending.version,
        "parts": pending.parts,
        "state": "claimed" if _claim_is_live(pending, thread) else "waiting",
        "thread_job_id": str(pending.thread_job_id) if pending.thread_job_id else None,
        "thread_job_state": job_state,
    }


def _serialize_locked(pending: PendingRequest) -> dict:
    job_state = (
        ThreadJob.objects.filter(id=pending.thread_job_id).values_list("state", flat=True).first()
    )
    return serialize(pending, Thread.objects.get(id=pending.thread_id), job_state)


def _serialize_loaded(pending: PendingRequest) -> dict:
    job = pending.thread_job
    return serialize(pending, pending.thread, job.state if job is not None else None)


def _lease_is_live(thread_id) -> bool:
    return Thread.objects.filter(
        id=thread_id, turn_lease_expires_at__isnull=False, turn_lease_expires_at__gt=Now()
    ).exists()


@sync_to_async
def ahold_message(thread_id, *, part_id: str, text: str) -> dict | None:
    """Add ``text`` to the thread's held request while its load is still queued.

    Returns the request, or None when the message must be answered as a normal turn
    instead: no load of this chat is waiting to resume it, the thread is answering
    now, or a stale claim needs a lease holder to settle it first.
    """
    with transaction.atomic():
        # The resume flips this row to RUNNING before it claims the request, so
        # locking it orders a hold before or after the whole claim, never between.
        job = (
            ThreadJob.objects.select_for_update()
            .filter(
                thread_id=thread_id,
                job_type=ThreadJob.JobType.MATERIALIZATION,
                state=ThreadJob.State.PENDING,
            )
            .order_by("-created_at")
            .first()
        )
        if job is None or _lease_is_live(thread_id):
            return None
        pending = PendingRequest.objects.select_for_update().filter(thread_id=thread_id).first()
        if pending is None:
            _check_length(text)
            pending = PendingRequest.objects.create(
                thread_id=thread_id, parts=[_new_part(part_id, text)], thread_job=job
            )
        elif pending.state == PendingRequest.State.CLAIMED:
            return None
        elif not any(part["id"] == part_id for part in pending.parts):
            parts = [*pending.parts, _new_part(part_id, text)]
            _check_length(combined_text(parts), len(parts))
            pending.parts = parts
            pending.version += 1
            pending.thread_job = job
            pending.save(update_fields=["parts", "version", "thread_job", "updated_at"])
        return _serialize_locked(pending)


@sync_to_async
def aadd_part(thread_id, *, part_id: str, text: str) -> dict:
    """Append a part; idempotent on ``part_id``. Raises ``PendingRequestConflict``
    once the request is claimed or gone, so the caller sends it as a turn instead."""
    with transaction.atomic():
        pending = PendingRequest.objects.select_for_update().filter(thread_id=thread_id).first()
        if pending is None:
            raise PendingRequestConflict("gone")
        if pending.state == PendingRequest.State.CLAIMED:
            raise PendingRequestConflict("claimed")
        if not any(part["id"] == part_id for part in pending.parts):
            parts = [*pending.parts, _new_part(part_id, text)]
            _check_length(combined_text(parts), len(parts))
            pending.parts = parts
            pending.version += 1
            pending.save(update_fields=["parts", "version", "updated_at"])
        return _serialize_locked(pending)


@sync_to_async
def adiscard(thread_id, *, version: int) -> None:
    with transaction.atomic():
        pending = (
            PendingRequest.objects.select_for_update()
            .select_related("thread")
            .filter(thread_id=thread_id)
            .first()
        )
        if pending is None:
            raise PendingRequestConflict("gone")
        if _claim_is_live(pending, pending.thread):
            raise PendingRequestConflict("claimed")
        if pending.version != version:
            raise PendingRequestConflict("version")
        pending.delete()


@sync_to_async
def _claim(thread_id, lease_token: uuid.UUID, thread_job_id) -> ClaimedRequest | _StaleClaim | None:
    with transaction.atomic():
        if not Thread.objects.filter(id=thread_id, turn_lease_token=lease_token).exists():
            return None
        pending = PendingRequest.objects.select_for_update().filter(thread_id=thread_id).first()
        if pending is None or not pending.parts:
            return None
        if thread_job_id is not None and pending.thread_job_id != thread_job_id:
            return None
        if pending.state == PendingRequest.State.CLAIMED and pending.claim_token != lease_token:
            return _StaleClaim(pending.request_id, pending.version, pending.claim_token)
        pending.state = PendingRequest.State.CLAIMED
        pending.claim_token = lease_token
        pending.save(update_fields=["state", "claim_token", "updated_at"])
        return ClaimedRequest(
            thread_id=str(thread_id),
            request_id=pending.request_id,
            version=pending.version,
            text=combined_text(pending.parts),
            token=lease_token,
        )


async def aclaim(thread_id, lease_token: uuid.UUID, *, thread_job_id=None) -> ClaimedRequest | None:
    """Claim the thread's held request for the run holding lease ``lease_token``.

    With ``thread_job_id`` (a load's resume), only a request held for that load: one
    left over from an earlier load is the user's to send, and the chat offers it so.
    """
    outcome = await _claim(thread_id, lease_token, thread_job_id)
    if not isinstance(outcome, _StaleClaim):
        return outcome
    # The stale claimant's run is over (we hold the lease), so its checkpoint says
    # whether the request was sent before it died.
    stale = PendingRequest.objects.filter(
        thread_id=thread_id, state=PendingRequest.State.CLAIMED, claim_token=outcome.token
    )
    if await athread_has_message(thread_id, held_message_id(outcome.request_id, outcome.version)):
        await stale.adelete()
        return None
    await stale.aupdate(state=PendingRequest.State.WAITING, claim_token=None)
    outcome = await _claim(thread_id, lease_token, thread_job_id)
    return outcome if isinstance(outcome, ClaimedRequest) else None


def _claimed_row(claimed: ClaimedRequest):
    return PendingRequest.objects.filter(
        thread_id=claimed.thread_id,
        state=PendingRequest.State.CLAIMED,
        claim_token=claimed.token,
    )


async def arelease(claimed: ClaimedRequest) -> None:
    """Return an unsent claim to waiting."""
    await _claimed_row(claimed).aupdate(state=PendingRequest.State.WAITING, claim_token=None)


def release_sync(claimed: ClaimedRequest) -> None:
    """``arelease`` for sync callers, such as Django closing a response it never sent."""
    _claimed_row(claimed).update(state=PendingRequest.State.WAITING, claim_token=None)


async def asettle(claimed: ClaimedRequest) -> None:
    """After the run: delete the request if its message is in the checkpoint, else unclaim it.

    Never raises. A claim left unsettled goes stale when the lease ends, and the
    next lease holder settles it the same way.
    """
    try:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            if await athread_has_message(claimed.thread_id, claimed.message_id):
                await PendingRequest.objects.filter(
                    thread_id=claimed.thread_id,
                    state=PendingRequest.State.CLAIMED,
                    claim_token=claimed.token,
                ).adelete()
            else:
                await arelease(claimed)
    except Exception:
        logger.warning(
            "Could not settle the held request of thread %s", claimed.thread_id, exc_info=True
        )


async def athread_has_message(thread_id, message_id: str) -> bool:
    checkpointer = await ensure_checkpointer()
    checkpoint_tuple = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": str(thread_id)}}
    )
    if checkpoint_tuple is None:
        return False
    messages = (checkpoint_tuple.checkpoint.get("channel_values") or {}).get("messages", [])
    return any(getattr(message, "id", None) == message_id for message in messages)


async def athread_pending_request(thread_id) -> dict | None:
    pending = (
        await PendingRequest.objects.select_related("thread", "thread_job")
        .filter(thread_id=thread_id)
        .afirst()
    )
    return _serialize_loaded(pending) if pending is not None else None


async def aworkspace_pending_requests(workspace, user) -> dict[str, dict]:
    """The user's held requests in ``workspace``, keyed by thread id."""
    return {
        str(pending.thread_id): _serialize_loaded(pending)
        async for pending in PendingRequest.objects.select_related("thread", "thread_job").filter(
            thread__workspace=workspace, thread__user=user
        )
    }
