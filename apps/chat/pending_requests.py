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
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Now
from django.utils import timezone
from langchain_core.messages import HumanMessage

from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.constants import MAX_MESSAGE_LENGTH, SYSTEM_RESUME_MARKER
from apps.chat.models import PendingRequest, Thread, ThreadJob
from apps.workspaces.services.load_activity import (
    aworkspace_own_build_pending,
    workspace_own_build_pending,
)

logger = logging.getLogger(__name__)

PART_SEPARATOR = "\n\n"
# AI SDK message ids are short; a longer client id is not one.
MAX_PART_ID_LENGTH = 128
# Each part is stored, locked and polled whole; the text cap alone allows thousands.
MAX_PARTS = 50
REQUEST_TOO_LONG_MESSAGE = "Request too long — edit it"
TOO_MANY_PARTS_MESSAGE = "That's as much as one request can hold — wait for your data"
# Fixed text for each refusal, so nothing but these strings reaches a response.
REFUSAL_MESSAGES = {
    "too_long": REQUEST_TOO_LONG_MESSAGE,
    "too_many_parts": TOO_MANY_PARTS_MESSAGE,
    "empty": "The request can't be empty — discard it instead",
    "first_part": "The first part can't be removed — edit it instead",
}
SETTLE_TIMEOUT_SECONDS = 15


class PendingRequestConflict(Exception):
    """The request was claimed, changed or settled since the caller last saw it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PendingRequestRefused(Exception):
    """A change the user must make differently; ``code`` names which, for its message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def user_message(self) -> str:
        return REFUSAL_MESSAGES[self.code]


class PendingRequestTooLong(PendingRequestRefused):
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
        raise PendingRequestTooLong("too_long")
    if part_count > MAX_PARTS:
        raise PendingRequestTooLong("too_many_parts")


def _new_part(part_id: str, text: str) -> dict:
    return {"id": part_id, "text": text, "added_at": timezone.now().isoformat()}


ACTIVE_JOB_STATES = ThreadJob.ACTIVE_STATES
# How long a stopped load that never started may still resume its chat (it
# resumes only if its queued job got as far as running); past that it never will.
CANCELLED_RESUME_WINDOW = timedelta(minutes=10)


def _job_active(job_id) -> bool:
    return (
        job_id is not None
        and ThreadJob.objects.filter(id=job_id, state__in=ACTIVE_JOB_STATES).exists()
    )


def _claim_is_live(pending: PendingRequest, thread: Thread) -> bool:
    return (
        pending.state == PendingRequest.State.CLAIMED
        and pending.claim_token is not None
        and thread.turn_lease_token == pending.claim_token
        and thread.turn_lease_expires_at is not None
        and thread.turn_lease_expires_at > timezone.now()
    )


def serialize(
    pending: PendingRequest,
    thread: Thread,
    job_state: str | None,
    workspace_loading: bool = False,
) -> dict:
    """The request as the chat shows it; a stale claim shows as waiting again.

    ``job_state`` is its load's ThreadJob state. A request with no load of its own
    waits on the workspace's (``workspace_loading``), whose end sends it.
    """
    return {
        "thread_id": str(pending.thread_id),
        "request_id": str(pending.request_id),
        "version": pending.version,
        "parts": pending.parts,
        "state": "claimed" if _claim_is_live(pending, thread) else "waiting",
        "thread_job_id": str(pending.thread_job_id) if pending.thread_job_id else None,
        "thread_job_state": job_state,
        "workspace_load_pending": workspace_loading,
    }


def _serialize_locked(pending: PendingRequest) -> dict:
    job_state = (
        ThreadJob.objects.filter(id=pending.thread_job_id).values_list("state", flat=True).first()
    )
    thread = Thread.objects.get(id=pending.thread_id)
    loading = pending.thread_job_id is None and workspace_own_build_pending(thread.workspace_id)
    return serialize(pending, thread, job_state, loading)


def _serialize_loaded(pending: PendingRequest, workspace_loading: bool = False) -> dict:
    job = pending.thread_job
    return serialize(
        pending,
        pending.thread,
        job.state if job is not None else None,
        workspace_loading and job is None,
    )


def _lease_is_live(thread_id) -> bool:
    return Thread.objects.filter(
        id=thread_id, turn_lease_expires_at__isnull=False, turn_lease_expires_at__gt=Now()
    ).exists()


@sync_to_async
def ahold_message(
    thread_id, *, part_id: str, text: str, for_workspace_load: bool = False
) -> dict | None:
    """Add ``text`` to the thread's held request while its load is still queued.

    Returns the request, or None when the message must be answered as a normal turn
    instead: no load is waiting to send it, the thread is answering now, or a stale
    claim needs a lease holder to settle it first. ``for_workspace_load`` holds it
    for a workspace load the chat did not start (the caller saw one pending), whose
    end flushes it.
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
        if (job is None and not for_workspace_load) or _lease_is_live(thread_id):
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
            # A workspace-load hold leaves a load of the chat's own in charge while
            # it runs; one that ended hands the request to the workspace load.
            if job is not None or not _job_active(pending.thread_job_id):
                pending.thread_job = job
            # New text is a new request to try sending.
            pending.flush_attempts = 0
            pending.save(
                update_fields=["parts", "version", "thread_job", "flush_attempts", "updated_at"]
            )
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
            pending.flush_attempts = 0
            pending.save(update_fields=["parts", "version", "flush_attempts", "updated_at"])
        return _serialize_locked(pending)


class PendingRequestInvalidEdit(PendingRequestRefused):
    pass


@sync_to_async
def aedit(
    thread_id, *, version: int, text: str | None = None, remove_part_id: str | None = None
) -> dict:
    """Replace the request's text, or remove one of its later parts, at ``version``.

    Raises ``PendingRequestConflict`` when the request changed, is being sent or is
    gone, since the edit was made against a copy that no longer stands. A claim
    that is stale still refuses: its message may already be in the conversation.
    """
    with transaction.atomic():
        pending = PendingRequest.objects.select_for_update().filter(thread_id=thread_id).first()
        if pending is None:
            raise PendingRequestConflict("gone")
        if pending.state == PendingRequest.State.CLAIMED:
            raise PendingRequestConflict("claimed")
        if pending.version != version:
            raise PendingRequestConflict("version")
        if text is not None:
            if not text.strip():
                raise PendingRequestInvalidEdit("empty")
            _check_length(text)
            # One part now: the user rewrote the whole request.
            pending.parts = [_new_part(f"edit-{uuid.uuid4()}", text)]
        else:
            index = next(
                (i for i, part in enumerate(pending.parts) if part["id"] == remove_part_id), None
            )
            if index is None:
                raise PendingRequestConflict("version")
            if index == 0:
                raise PendingRequestInvalidEdit("first_part")
            pending.parts = [part for i, part in enumerate(pending.parts) if i != index]
        pending.version += 1
        if text is not None:
            # A rewrite is a new request to try sending; a removal only shortens it.
            pending.flush_attempts = 0
        pending.save(update_fields=["parts", "version", "flush_attempts", "updated_at"])
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
        # A load's resume also takes a request held for the workspace: its chat's
        # own load ending is the workspace load ending for it.
        if thread_job_id is not None and pending.thread_job_id not in (thread_job_id, None):
            return None
        # Not while it still waits on another load of the workspace: this resume
        # (perhaps of a stopped load) would answer it without that data.
        if (
            thread_job_id is not None
            and pending.thread_job_id is None
            and workspace_own_build_pending(Thread.objects.get(id=thread_id).workspace_id)
        ):
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


async def athread_has_user_turn(thread_id) -> bool:
    """Whether the conversation holds anything the user wrote (resume notices aside)."""
    checkpointer = await ensure_checkpointer()
    checkpoint_tuple = await checkpointer.aget_tuple(
        {"configurable": {"thread_id": str(thread_id)}}
    )
    if checkpoint_tuple is None:
        return False
    messages = (checkpoint_tuple.checkpoint.get("channel_values") or {}).get("messages", [])
    return any(
        isinstance(message, HumanMessage)
        and not str(message.content).startswith(SYSTEM_RESUME_MARKER)
        for message in messages
    )


async def athread_pending_request(thread_id) -> dict | None:
    pending = (
        await PendingRequest.objects.select_related("thread", "thread_job")
        .filter(thread_id=thread_id)
        .afirst()
    )
    if pending is None:
        return None
    loading = pending.thread_job_id is None and await aworkspace_own_build_pending(
        pending.thread.workspace_id
    )
    return _serialize_loaded(pending, loading)


async def aworkspace_pending_requests(workspace, user) -> dict[str, dict]:
    """The user's held requests in ``workspace``, keyed by thread id."""
    held = [
        pending
        async for pending in PendingRequest.objects.select_related("thread", "thread_job").filter(
            thread__workspace=workspace, thread__user=user
        )
    ]
    loading = any(p.thread_job_id is None for p in held) and await aworkspace_own_build_pending(
        workspace.id
    )
    return {str(pending.thread_id): _serialize_loaded(pending, loading) for pending in held}


MAX_FLUSH_ATTEMPTS = 1


def _flushable(*, skip_answering: bool = True):
    """Requests held for a workspace load, which no load of their chat sends.

    One left after its own load ended is not among them: the chat offers it to
    the user to send, and that load's resume may still be coming.
    """
    # A chat whose own load is under way, or stopped with its resume still to run
    # (CANCELLED, never started), is that resume's to answer: it takes these too.
    resume_coming = ThreadJob.objects.filter(
        Q(state__in=ACTIVE_JOB_STATES)
        | Q(
            state=ThreadJob.State.CANCELLED,
            started_at__isnull=True,
            completed_at__gt=timezone.now() - CANCELLED_RESUME_WINDOW,
        )
    )
    flushable = PendingRequest.objects.filter(
        thread_job__isnull=True, flush_attempts__lt=MAX_FLUSH_ATTEMPTS
    ).exclude(thread__jobs__in=resume_coming)
    if skip_answering:
        # Answering now: that turn takes the request with it.
        flushable = flushable.exclude(thread__turn_lease_expires_at__gt=Now())
    return flushable


async def aflushable_thread_ids(workspace_id) -> list:
    """Oldest first, so a batch never leaves the longest-waiting request behind."""
    return [
        thread_id
        async for thread_id in _flushable()
        .filter(thread__workspace_id=workspace_id)
        .order_by("created_at")
        .values_list("thread_id", flat=True)
    ]


async def aflushable_workspace_ids() -> set:
    return {
        workspace_id
        async for workspace_id in _flushable().values_list("thread__workspace_id", flat=True)
    }


async def acount_flush_attempt(thread_id) -> bool:
    """Spend one of the request's flush attempts; False when it has none left (or is gone)."""
    return bool(
        # The flush holds the thread's lease by now, so the thread counts as answering.
        await _flushable(skip_answering=False)
        .filter(thread_id=thread_id)
        .aupdate(flush_attempts=F("flush_attempts") + 1)
    )
