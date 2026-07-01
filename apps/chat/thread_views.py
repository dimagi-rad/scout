"""Thread CRUD endpoints: list, messages, share, public."""

import json
import logging
from datetime import UTC, datetime

from django.http import JsonResponse

from apps.chat.artifact_links import (
    backfill_thread_artifact_links,
    serialize_thread_artifact_link,
)
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    CheckpointerUnavailable,
    _resolve_workspace_and_membership,
    async_login_required,
)
from apps.chat.message_converter import langchain_messages_to_ui
from apps.chat.models import Thread, ThreadArtifact

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


async def _get_public_thread(share_token):
    """Load a shared thread by share token."""
    try:
        return await Thread.objects.select_related("user").aget(
            share_token=share_token, is_shared=True
        )
    except Thread.DoesNotExist:
        return None


async def _update_thread_sharing(thread, is_shared=None):
    """Update sharing settings on a thread."""
    if is_shared is not None:
        thread.is_shared = is_shared
    await thread.asave()
    return {
        "id": str(thread.id),
        "is_shared": thread.is_shared,
        "is_public": thread.is_shared,
        "share_token": thread.share_token,
    }


def _thread_summary(thread, *, history_title: str | None = None):
    display_title = _display_thread_title(thread)
    return {
        "id": str(thread.id),
        "title": display_title or "Untitled",
        "history_title": history_title or _history_thread_title(thread),
        "title_is_custom": thread.title_is_custom,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
        "is_shared": thread.is_shared,
        "is_public": thread.is_shared,
        "share_token": thread.share_token,
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


async def _get_thread_artifacts(thread_id):
    """Load artifacts associated with a thread.

    Returns the artifact ``code`` and ``data`` so a public (unauthenticated)
    thread page can render each artifact in a client-side sandboxed iframe
    (``srcdoc``) instead of dumping the source as ``<pre>``.

    Note: the authenticated server sandbox route
    (``/api/workspaces/<wsid>/artifacts/<id>/sandbox/``) and the live
    ``query-data`` route both require session auth + workspace membership, so
    they intentionally are NOT exposed here. Public rendering uses the embedded
    static ``data`` only; live tenant data is never served to anonymous viewers.
    """
    thread = await Thread.objects.filter(id=thread_id).select_related("workspace").afirst()
    if thread is None:
        return []
    await backfill_thread_artifact_links(thread)
    queryset = (
        ThreadArtifact.objects.filter(thread=thread)
        .select_related("artifact")
        .order_by("artifact__created_at")
    )
    return [
        {
            "id": str(link.artifact.id),
            "title": link.artifact.title,
            "artifact_type": link.artifact.artifact_type,
            "code": link.artifact.code,
            "data": link.artifact.data,
            "version": link.artifact.version,
        }
        async for link in queryset
    ]


async def _list_threads(user, *, workspace_id):
    """Return recent threads for a workspace/user."""
    from apps.workspaces.workspace_resolver import aresolve_workspace

    workspace, _err = await aresolve_workspace(user, workspace_id)
    if workspace is None:
        return None

    summaries = []
    queryset = Thread.objects.filter(user=user, workspace=workspace).order_by("-updated_at")[:50]
    async for thread in queryset:
        summaries.append(await _thread_summary_for_response(thread))
    return summaries


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

    threads = await _list_threads(user, workspace_id=workspace_id)
    if threads is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)
    return JsonResponse(threads, safe=False)


@async_login_required
async def thread_detail_view(request, workspace_id, thread_id):
    """GET/PATCH /api/workspaces/<workspace_id>/threads/<thread_id>/."""

    user = request._authenticated_user
    workspace, _, _ = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)

    if request.method == "GET":
        if thread is None:
            return JsonResponse({"error": "Thread not found"}, status=404)
        return JsonResponse(await _thread_summary_for_response(thread))

    if request.method == "PATCH":
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
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

    workspace, _, _is_multi = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        # Distinguish a brand-new chat from a stale/foreign one. New chats use
        # client-generated UUIDs with no Thread row until the first POST, so a
        # missing row must keep returning [] 200. But if a row *exists* and just
        # doesn't belong to this (user, workspace), it's a stale/cross-workspace
        # thread — return 404 so the client can recover instead of silently
        # showing an empty, "haunted" chat.
        if await Thread.objects.filter(id=thread_id).aexists():
            return JsonResponse({"error": "Thread not found"}, status=404)
        return JsonResponse([], safe=False)

    try:
        ui_messages = await _load_thread_messages(thread_id)
    except CheckpointerUnavailable:
        # A checkpointer/DB blip — surface it as a retryable error instead of an
        # empty list that reads as "conversation deleted" (07#7).
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
    workspace, _, _ = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"results": []})

    await backfill_thread_artifact_links(thread)
    queryset = (
        ThreadArtifact.objects.filter(thread=thread)
        .select_related("artifact")
        .order_by("-last_seen_at")
    )
    return JsonResponse(
        {"results": [serialize_thread_artifact_link(link) async for link in queryset]}
    )


@async_login_required
async def thread_share_view(request, workspace_id, thread_id):
    """
    GET  /api/chat/threads/<thread_id>/share/  — get sharing settings
    PATCH /api/chat/threads/<thread_id>/share/ — update sharing settings
    """
    user = request._authenticated_user

    workspace, _, _is_multi = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    thread = await _get_thread(thread_id, user, workspace_id=workspace_id)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    if request.method == "GET":
        return JsonResponse(
            {
                "id": str(thread.id),
                "is_shared": thread.is_shared,
                "share_token": thread.share_token,
            }
        )

    if request.method == "PATCH":
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        result = await _update_thread_sharing(
            thread,
            is_shared=body.get("is_shared"),
        )
        return JsonResponse(result)

    return JsonResponse({"error": "Method not allowed"}, status=405)


@async_login_required
async def thread_viewed_view(request, workspace_id, thread_id):
    """POST /api/workspaces/<workspace_id>/threads/<thread_id>/viewed/

    Update Thread.last_viewed_at to now. Called by the frontend when the user
    opens a thread; clears the green-dot unread indicator.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, _, _ = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    updated = await Thread.objects.filter(
        id=thread_id,
        user=user,
        workspace=workspace,
    ).aupdate(last_viewed_at=datetime.now(UTC))
    if not updated:
        return JsonResponse({"error": "Thread not found"}, status=404)
    return JsonResponse({"status": "ok"})


async def public_thread_view(request, share_token):
    """
    GET /api/chat/threads/shared/<share_token>/

    Public read-only view of a shared thread's messages and artifacts.
    No authentication required.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    thread = await _get_public_thread(share_token)
    if thread is None:
        return JsonResponse({"error": "Thread not found"}, status=404)

    # Load messages from checkpointer
    try:
        messages = await _load_thread_messages(thread.id)
    except CheckpointerUnavailable:
        return JsonResponse(
            {"error": "Conversation history is temporarily unavailable. Please try again."},
            status=503,
        )

    # Load associated artifacts
    artifacts = await _get_thread_artifacts(thread.id)

    return JsonResponse(
        {
            "thread": {
                "id": str(thread.id),
                "title": _thread_summary(thread)["title"],
                "created_at": thread.created_at.isoformat(),
            },
            "messages": messages,
            "artifacts": artifacts,
        }
    )
