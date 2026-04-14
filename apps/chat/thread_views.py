"""Thread CRUD endpoints: list, messages, share, public."""

import json
import logging

from django.http import JsonResponse

from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    _resolve_workspace_and_membership,
    async_login_required,
)
from apps.chat.message_converter import langchain_messages_to_ui
from apps.chat.models import Thread

logger = logging.getLogger(__name__)


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
        "share_token": thread.share_token,
    }


async def _get_thread_artifacts(thread_id):
    """Load artifacts associated with a thread."""
    from apps.artifacts.models import Artifact

    return [
        {
            "id": str(a.id),
            "title": a.title,
            "artifact_type": a.artifact_type,
            "code": a.code,
            "data": a.data,
            "version": a.version,
        }
        async for a in Artifact.objects.filter(conversation_id=str(thread_id)).order_by(
            "created_at"
        )
    ]


async def _list_threads(user, *, workspace_id):
    """Return recent threads for a workspace/user."""
    from apps.workspaces.workspace_resolver import aresolve_workspace

    workspace, err = await aresolve_workspace(user, workspace_id)
    if workspace is None:
        return None

    return [
        {
            "id": str(t.id),
            "title": t.title,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "is_shared": t.is_shared,
        }
        async for t in Thread.objects.filter(user=user, workspace=workspace).order_by(
            "-updated_at"
        )[:50]
    ]


async def _load_thread_messages(thread_id) -> list[dict]:
    """Load messages from checkpointer and convert to UI format."""
    try:
        checkpointer = await ensure_checkpointer()
        config = {"configurable": {"thread_id": str(thread_id)}}
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("Failed to load checkpoint for thread %s", thread_id, exc_info=True)
        return []

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
        return JsonResponse([], safe=False)

    ui_messages = await _load_thread_messages(thread_id)
    return JsonResponse(ui_messages, safe=False)


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
    messages = await _load_thread_messages(thread.id)

    # Load associated artifacts
    artifacts = await _get_thread_artifacts(thread.id)

    return JsonResponse(
        {
            "thread": {
                "id": str(thread.id),
                "title": thread.title,
                "created_at": thread.created_at.isoformat(),
            },
            "messages": messages,
            "artifacts": artifacts,
        }
    )
