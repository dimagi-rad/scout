"""Thread CRUD endpoints: list, detail, messages, artifacts, viewed."""

import logging
from datetime import UTC, datetime

from django.http import JsonResponse

from apps.chat.artifact_links import (
    backfill_thread_artifact_links,
    latest_version_links,
    serialize_thread_artifact_link,
)
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    CheckpointerUnavailable,
    async_login_required,
)
from apps.chat.message_converter import langchain_messages_to_ui
from apps.chat.models import Thread, ThreadArtifact
from apps.common.http import parse_json_object
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)


THREAD_TITLE_PREVIEW_CHARS = 200


async def _get_thread(thread_id, user, *, workspace_id=None):
    """Load a thread ensuring ownership, optionally scoped to a workspace."""
    try:
        if workspace_id is not None:
            return await Thread.objects.aget(id=thread_id, user=user, workspace_id=workspace_id)
        return await Thread.objects.aget(id=thread_id, user=user)
    except Thread.DoesNotExist:
        return None


def _thread_summary(thread, *, history_title: str | None = None):
    display_title = _display_thread_title(thread)
    return {
        "id": str(thread.id),
        "title": display_title or "Untitled",
        "history_title": history_title or _history_thread_title(thread),
        "title_is_custom": thread.title_is_custom,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
        "last_viewed_at": thread.last_viewed_at.isoformat() if thread.last_viewed_at else None,
    }


def _short_thread_title(title: str) -> str:
    clean = title.strip()
    if len(clean) > THREAD_TITLE_PREVIEW_CHARS:
        return f"{clean[:THREAD_TITLE_PREVIEW_CHARS].rstrip()}..."
    return clean


def _display_thread_title(thread) -> str:
    if not thread.title_is_custom:
        return "Untitled"
    return _short_thread_title(thread.title) or "Untitled"


def _history_thread_title(thread) -> str:
    if thread.title_is_custom:
        return _display_thread_title(thread)
    return _short_thread_title(thread.title) or "Untitled"


async def _thread_summary_for_response(thread):
    history_title = _history_thread_title(thread)
    if not thread.title_is_custom and history_title == "Untitled":
        history_title = await _first_user_message_title(thread.id) or history_title
    return _thread_summary(thread, history_title=history_title)


async def _first_user_message_title(thread_id) -> str:
    try:
        messages = await _load_thread_messages(thread_id)
    except CheckpointerUnavailable:
        return ""
    for message in messages:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        if content:
            return _short_thread_title(content)
        parts = message.get("parts") or []
        text = " ".join(
            str(part.get("text") or "").strip()
            for part in parts
            if part.get("type") == "text" and part.get("text")
        ).strip()
        if text:
            return _short_thread_title(text)
    return ""


async def _list_threads(user, *, workspace_id):
    """Return ``(threads, error_response)`` for a workspace/user.

    ``error_response`` is a ready-to-return 403 ``JsonResponse`` (generic, or the
    lost-upstream-access variant) when access is denied; ``None`` on success.
    """
    from apps.workspaces.workspace_resolver import aresolve_workspace

    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return None, err

    summaries = []
    queryset = Thread.objects.filter(user=user, workspace=workspace).order_by("-updated_at")[:50]
    async for thread in queryset:
        summaries.append(await _thread_summary_for_response(thread))
    return summaries, None


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
        return JsonResponse(await _thread_summary_for_response(thread))

    if request.method == "PATCH":
        body, err = parse_json_object(request)
        if err:
            return err
        title = _short_thread_title(str(body.get("title", "")))
        if thread is None:
            thread = Thread(
                id=thread_id,
                user=user,
                workspace=workspace,
                title=title,
                title_is_custom=bool(title),
            )
            await thread.asave()
        else:
            thread.title = title
            thread.title_is_custom = bool(title)
            await thread.asave(update_fields=["title", "title_is_custom", "updated_at"])
        return JsonResponse(await _thread_summary_for_response(thread))

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

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        # New chats use client-generated UUIDs with no row until first POST, so a
        # missing row returns [] 200. A row that exists but isn't this (user, workspace)
        # is stale/cross-workspace — 404 so the client recovers instead of showing
        # an empty "haunted" chat.
        if await Thread.objects.filter(id=thread_id).aexists():
            return JsonResponse({"error": "Thread not found"}, status=404)
        return JsonResponse([], safe=False)

    try:
        ui_messages = await _load_thread_messages(thread_id)
    except CheckpointerUnavailable:
        # Retryable error, not an empty list that reads as "conversation deleted" (07#7).
        return JsonResponse(
            {"error": "Conversation history is temporarily unavailable. Please try again."},
            status=503,
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
