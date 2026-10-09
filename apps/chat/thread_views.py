"""Thread CRUD endpoints: list, detail, messages, artifacts, viewed."""

import logging
from datetime import UTC, datetime

from django.db.models import BooleanField, ExpressionWrapper, Q
from django.db.models.functions import Now
from django.http import JsonResponse

from apps.chat import pending_requests, resume_stream
from apps.chat.artifact_links import (
    backfill_thread_artifact_links,
    latest_version_links,
    serialize_thread_artifact_link,
)
from apps.chat.checkpointer import athread_has_checkpoint, ensure_checkpointer
from apps.chat.helpers import (
    CheckpointerUnavailable,
    async_login_required,
)
from apps.chat.message_converter import langchain_messages_to_ui
from apps.chat.models import Thread, ThreadArtifact
from apps.chat.titles import afill_blank_titles, afirst_user_message, short_thread_title
from apps.common.http import parse_json_object
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)


# An agent run holds the thread's turn lease (a chat turn in any tab, or a background
# resume); a holder that died clears once its lease lapses. Judged by the database
# clock, as the lease itself is (apps/chat/turn_lease.py).
_TURN_RUNNING = ExpressionWrapper(Q(turn_lease_expires_at__gt=Now()), output_field=BooleanField())


def _threads():
    return Thread.objects.annotate(lease_live=_TURN_RUNNING)


async def _get_thread(thread_id, user, *, workspace_id=None):
    """Load a thread ensuring ownership, optionally scoped to a workspace."""
    try:
        if workspace_id is not None:
            return await _threads().aget(id=thread_id, user=user, workspace_id=workspace_id)
        return await _threads().aget(id=thread_id, user=user)
    except Thread.DoesNotExist:
        return None


async def _thread_id_taken(thread_id) -> bool:
    """True when ``thread_id`` belongs to another user's row or to a deleted thread's
    checkpoints, so the caller may not create a Thread under it."""
    return await Thread.objects.filter(id=thread_id).aexists() or await athread_has_checkpoint(
        thread_id
    )


def _turn_running(thread) -> bool:
    # NULL (no lease) compares as NULL; a row made here, not loaded, has no lease.
    return getattr(thread, "lease_live", None) is True


def _thread_summary(thread):
    # Blank stays blank: the client owns the "Untitled" placeholder, so it can tell a
    # placeholder from a real title.
    title = short_thread_title(thread.title)
    return {
        "id": str(thread.id),
        "title": title,
        # Same value as title; kept for browser tabs loaded before titles were unified.
        "history_title": title,
        "title_is_custom": thread.title_is_custom,
        "title_source": thread.title_source,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
        "last_viewed_at": thread.last_viewed_at.isoformat() if thread.last_viewed_at else None,
        "turn_running": _turn_running(thread),
    }


async def _list_threads(user, *, workspace_id):
    """Return ``(threads, error_response)`` for a workspace/user.

    ``error_response`` is a ready-to-return 403 ``JsonResponse`` (generic, or the
    lost-upstream-access variant) when access is denied; ``None`` on success.
    """

    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return None, err

    queryset = _threads().filter(user=user, workspace=workspace).order_by("-updated_at")[:50]
    threads = [thread async for thread in queryset]
    await afill_blank_titles(threads)
    return [_thread_summary(thread) for thread in threads], None


async def _load_thread_messages(thread_id) -> list[dict]:
    """Load messages from checkpointer and convert to UI format.

    Raises ``CheckpointerUnavailable`` on a checkpointer/DB error so the caller
    can return a non-200 error: an empty list is reserved for a thread that is
    genuinely empty (no checkpoint written yet). Swallowing the error into []
    made a transient outage look like "the conversation was deleted" (07#7).
    """
    try:
        checkpointer = await ensure_checkpointer()
        config = {"configurable": {"thread_id": str(thread_id)}}
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception as exc:
        logger.warning("Failed to load checkpoint for thread %s", thread_id, exc_info=True)
        raise CheckpointerUnavailable(str(exc)) from exc

    if checkpoint_tuple is None:
        return []

    checkpoint = checkpoint_tuple.checkpoint
    lc_messages = (checkpoint.get("channel_values") or {}).get("messages", [])
    return langchain_messages_to_ui(lc_messages)


@async_login_required
async def thread_list_view(request, workspace_id):
    """
    GET /api/workspaces/<workspace_id>/threads/

    Returns recent threads for the authenticated user in a workspace.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    threads, err = await _list_threads(user, workspace_id=workspace_id)
    if err is not None:
        return err
    return JsonResponse(threads, safe=False)


@async_login_required
async def thread_detail_view(request, workspace_id, thread_id):
    """GET/PATCH /api/workspaces/<workspace_id>/threads/<thread_id>/."""

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)

    if request.method == "GET":
        if thread is None:
            return JsonResponse({"error": "Thread not found"}, status=404)
        await afill_blank_titles([thread])
        return JsonResponse(_thread_summary(thread))

    if request.method == "PATCH":
        body, err = parse_json_object(request)
        if err:
            return err
        title = short_thread_title(str(body.get("title", "")))
        if thread is None:
            if await _thread_id_taken(thread_id):
                return JsonResponse({"error": "Thread not found"}, status=404)
            thread = Thread(id=thread_id, user=user, workspace=workspace)
        if title:
            # A rename is final: generation never overwrites a USER title.
            thread.title = title
            thread.title_is_custom = True
            thread.title_source = Thread.TitleSource.USER
        else:
            # Clearing hands the title back to the automatic flow: the first message
            # now, and a generated title after the next successful turn.
            thread.title = short_thread_title(await afirst_user_message(thread.id))
            thread.title_is_custom = False
            thread.title_source = Thread.TitleSource.FIRST_MESSAGE
        if thread._state.adding:
            await thread.asave()
        else:
            await thread.asave(
                update_fields=["title", "title_is_custom", "title_source", "updated_at"]
            )
        return JsonResponse(_thread_summary(thread))

    return JsonResponse({"error": "Method not allowed"}, status=405)


@async_login_required
async def thread_messages_view(request, workspace_id, thread_id):
    """
    GET /api/chat/threads/<thread_id>/messages/

    Loads conversation from the checkpointer and returns UIMessage format.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    _workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err

    # Opt-in, so a client from before held requests still gets the bare list.
    with_pending = request.GET.get("include") == "pending"
    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        # New chats use client-generated UUIDs with no row until first POST, so a
        # missing row returns [] 200. Another user's row, or a deleted thread's id, is
        # stale — 404 so the client recovers instead of showing an empty "haunted" chat.
        if await _thread_id_taken(thread_id):
            return JsonResponse({"error": "Thread not found"}, status=404)
        if with_pending:
            return JsonResponse({"messages": [], "pending_request": None, "turn_running": False})
        return JsonResponse([], safe=False)

    try:
        ui_messages = await _load_thread_messages(thread_id)
    except CheckpointerUnavailable:
        # Retryable error, not an empty list that reads as "conversation deleted" (07#7).
        return JsonResponse(
            {"error": "Conversation history is temporarily unavailable. Please try again."},
            status=503,
        )
    if with_pending:
        return JsonResponse(
            {
                "messages": ui_messages,
                "pending_request": await pending_requests.athread_pending_request(thread.id),
                # From the row read before the checkpoint, so a turn that ends in
                # between reads as still running (the client polls again), never as
                # finished with its answer missing.
                "turn_running": _turn_running(thread),
            }
        )
    return JsonResponse(ui_messages, safe=False)


@async_login_required
async def thread_artifacts_view(request, workspace_id, thread_id):
    """GET /api/workspaces/<workspace_id>/threads/<thread_id>/artifacts/."""

    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    _workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"results": []})

    await backfill_thread_artifact_links(thread)
    queryset = (
        ThreadArtifact.objects.filter(thread=thread, artifact__is_deleted=False)
        .select_related("artifact")
        .order_by("-last_seen_at")
    )
    links = [link async for link in queryset]
    return JsonResponse(
        {"results": [serialize_thread_artifact_link(link) for link in latest_version_links(links)]}
    )


@async_login_required
async def thread_viewed_view(request, workspace_id, thread_id):
    """POST /api/workspaces/<workspace_id>/threads/<thread_id>/viewed/

    Update Thread.last_viewed_at to now. Called by the frontend when the user
    opens a thread; clears the green-dot unread indicator.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err

    updated = await Thread.objects.filter(
        id=thread_id,
        user=user,
        workspace=workspace,
    ).aupdate(last_viewed_at=datetime.now(UTC))
    if not updated:
        return JsonResponse({"error": "Thread not found"}, status=404)
    return JsonResponse({"status": "ok"})


def _pending_conflict(error: pending_requests.PendingRequestConflict) -> JsonResponse:
    return JsonResponse({"error": "pending_request_conflict", "reason": error.reason}, status=409)


@async_login_required
async def thread_pending_request_parts_view(request, workspace_id, thread_id):
    """POST /api/workspaces/<workspace_id>/threads/<thread_id>/pending-request/parts/

    Adds ``{id, text}`` to the request held while the chat's data loads; idempotent
    on ``id``. A 409 means the request was claimed or is gone, and the client sends
    the text as a normal message instead.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    _workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err
    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    body, err = parse_json_object(request)
    if err:
        return err
    part_id, text = body.get("id"), body.get("text")
    if (
        not isinstance(part_id, str)
        or not part_id
        or len(part_id) > pending_requests.MAX_PART_ID_LENGTH
    ):
        return JsonResponse({"error": "id must be a non-empty string"}, status=400)
    if not isinstance(text, str) or not text.strip():
        return JsonResponse({"error": "text must be a non-empty string"}, status=400)
    try:
        pending = await pending_requests.aadd_part(thread.id, part_id=part_id, text=text)
    except pending_requests.PendingRequestConflict as e:
        return _pending_conflict(e)
    except pending_requests.PendingRequestTooLong as e:
        return JsonResponse(
            {
                "error": e.user_message,
                "reason": "pending_request_too_long",
            },
            status=400,
        )
    return JsonResponse(pending)


@async_login_required
async def thread_pending_request_view(request, workspace_id, thread_id):
    """PATCH/DELETE /api/workspaces/<workspace_id>/threads/<thread_id>/pending-request/

    PATCH ``{version, text}`` rewrites the held request and ``{version,
    remove_part_id}`` removes one of its later parts; DELETE ``{version}`` discards
    it. A 409 means it changed, was claimed or is gone.
    """
    if request.method not in ("PATCH", "DELETE"):
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    _workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err
    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    body, err = parse_json_object(request)
    if err:
        return err
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return JsonResponse({"error": "version must be an integer"}, status=400)
    if request.method == "PATCH":
        return await _edit_pending_request(thread, version, body)
    try:
        await pending_requests.adiscard(thread.id, version=version)
    except pending_requests.PendingRequestConflict as e:
        return _pending_conflict(e)
    return JsonResponse({"status": "discarded"})


async def _edit_pending_request(thread, version: int, body: dict) -> JsonResponse:
    text, remove_part_id = body.get("text"), body.get("remove_part_id")
    if (text is None) == (remove_part_id is None):
        return JsonResponse({"error": "Send exactly one of text or remove_part_id"}, status=400)
    if text is not None and not isinstance(text, str):
        return JsonResponse({"error": "text must be a string"}, status=400)
    if remove_part_id is not None and not isinstance(remove_part_id, str):
        return JsonResponse({"error": "remove_part_id must be a string"}, status=400)
    try:
        pending = await pending_requests.aedit(
            thread.id, version=version, text=text, remove_part_id=remove_part_id
        )
    except pending_requests.PendingRequestConflict as e:
        return _pending_conflict(e)
    except pending_requests.PendingRequestTooLong as e:
        return JsonResponse(
            {"error": e.user_message, "reason": "pending_request_too_long"}, status=400
        )
    except pending_requests.PendingRequestInvalidEdit as e:
        return JsonResponse(
            {"error": e.user_message, "reason": "pending_request_invalid_edit"}, status=400
        )
    return JsonResponse(pending)


@async_login_required
async def thread_resume_stream_view(request, workspace_id, thread_id):
    """GET /api/workspaces/<workspace_id>/threads/<thread_id>/resume-stream/?after=<id>

    The text a background resume of this thread has streamed since row ``after``:
    ``{"chunks": [{id, run, text, done}]}``, oldest first. A chat tails it while
    the resume runs and reloads the thread's messages once the run is done.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    user = request._authenticated_user
    _workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err
    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"chunks": []})
    try:
        after = max(int(request.GET.get("after", "0")), 0)
    except ValueError:
        return JsonResponse({"error": "after must be an integer"}, status=400)
    chunks = await resume_stream.aread_after(thread.id, after)
    return JsonResponse({"chunks": chunks, "more": len(chunks) >= resume_stream.READ_LIMIT})
