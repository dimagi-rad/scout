"""
Chat views: streaming chat endpoint.

The chat endpoint is a raw async Django view (not DRF) because DRF
does not support async streaming responses.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    _resolve_chat_access,
    async_login_required,
    repair_dangling_tool_calls,
)
from apps.chat.models import Thread, ThreadJob
from apps.chat.rate_limiting import chat_rate_limit
from apps.chat.stream import langgraph_to_ui_stream
from apps.common.http import parse_json_object
from apps.workspaces.access import access_denied_body
from apps.workspaces.services.workspace_service import touch_workspace_schemas

logger = logging.getLogger(__name__)

THREAD_TITLE_PREVIEW_CHARS = 200


def _short_thread_title(title: str) -> str:
    clean = title.strip()
    if len(clean) > THREAD_TITLE_PREVIEW_CHARS:
        return f"{clean[:THREAD_TITLE_PREVIEW_CHARS].rstrip()}..."
    return clean


async def _upsert_thread(thread_id, user, history_title: str = "", *, workspace) -> Thread:
    """Create the Thread row if absent and bump updated_at on every turn.

    Returns the row so the caller can re-check ownership: a row another user
    created between the caller's lookup and this call is returned untouched,
    never adopted.

    The explicit ``updated_at`` bump is load-bearing: without it the sidebar's
    "newer than last_viewed" indicator and ``-updated_at`` ordering freeze at
    the creation timestamp.
    """
    thread, created = await Thread.objects.aget_or_create(
        id=thread_id,
        defaults={
            "user": user,
            "workspace": workspace,
            "title": _short_thread_title(history_title),
            "title_is_custom": False,
        },
    )
    if not created and not _is_foreign_thread(thread, user, workspace):
        thread.updated_at = timezone.now()
        await Thread.objects.filter(pk=thread.pk).aupdate(updated_at=thread.updated_at)
    return thread


def _canonical_thread_id(value) -> str | None:
    """The canonical UUID string for a client-supplied thread id, or None if it isn't one.

    The checkpointer keys state on this string alone, with no user or workspace
    scoping, so it must be exactly the Thread row's id that the ownership check
    ran against.
    """
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _is_foreign_thread(thread: Thread, user, workspace) -> bool:
    return thread.user_id != user.pk or thread.workspace_id != workspace.pk


def _foreign_thread_response(thread: Thread, user, workspace) -> JsonResponse:
    logger.warning(
        "Rejected chat POST to foreign thread: thread_id=%s requesting_user=%s "
        "owner_user=%s thread_workspace=%s requested_workspace=%s",
        thread.id,
        user.pk,
        thread.user_id,
        thread.workspace_id,
        workspace.pk,
    )
    return JsonResponse({"error": "Thread not found"}, status=404)


MAX_MESSAGE_LENGTH = 10_000


def _last_message_text(message) -> tuple[str | None, JsonResponse | None]:
    """The text of the turn's last message, or a 400 when its shape isn't one the AI SDK sends.

    AI SDK v6 sends ``{parts: [{type: "text", text: "..."}]}`` instead of ``{content: "..."}``.
    """
    if not isinstance(message, dict):
        return None, JsonResponse({"error": "Each message must be an object"}, status=400)
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return None, JsonResponse({"error": "Message content must be a string"}, status=400)
    if content:
        return content, None
    parts = message.get("parts")
    if parts is None:
        return "", None
    if not isinstance(parts, list) or not all(isinstance(p, dict) for p in parts):
        return None, JsonResponse({"error": "Message parts must be a list of objects"}, status=400)
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    if not all(isinstance(t, str) for t in texts):
        return None, JsonResponse({"error": "Text part text must be a string"}, status=400)
    return " ".join(texts), None


@csrf_protect
@async_login_required
@chat_rate_limit
async def chat_view(request):
    """
    POST /api/chat/

    Accepts Vercel AI SDK useChat request format, returns a
    StreamingHttpResponse in the Data Stream Protocol.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    body, err = parse_json_object(request)
    if err:
        return err

    messages = body.get("messages", [])
    data = body.get("data", {})
    if not isinstance(data, dict):
        return JsonResponse({"error": "data must be an object"}, status=400)
    if not isinstance(messages, list):
        return JsonResponse({"error": "messages must be a list"}, status=400)
    workspace_id = data.get("workspaceId") or body.get("workspaceId")
    raw_thread_id = data.get("threadId") or body.get("threadId") or str(uuid.uuid4())

    if not messages:
        return JsonResponse({"error": "messages is required"}, status=400)
    if not workspace_id:
        return JsonResponse({"error": "workspaceId is required"}, status=400)
    if not isinstance(workspace_id, str):
        return JsonResponse({"error": "workspaceId must be a string"}, status=400)
    if not isinstance(raw_thread_id, str):
        return JsonResponse({"error": "threadId must be a string"}, status=400)
    thread_id = _canonical_thread_id(raw_thread_id)
    if thread_id is None:
        return JsonResponse({"error": "threadId must be a UUID"}, status=400)

    user_content, err = _last_message_text(messages[-1])
    if err:
        return err
    if not user_content or not user_content.strip():
        return JsonResponse({"error": "Empty message"}, status=400)
    if len(user_content) > MAX_MESSAGE_LENGTH:
        return JsonResponse(
            {"error": f"Message exceeds {MAX_MESSAGE_LENGTH} characters"}, status=400
        )

    # Resolve workspace and verify access. The multi-tenant flag is determined
    # in a single DB read inside _resolve_chat_access to avoid TOCTOU.
    access, tm, is_multi_tenant = await _resolve_chat_access(user, workspace_id)
    workspace = access.workspace
    if workspace is None:
        return JsonResponse(access_denied_body(access), status=403)

    if tm is None and not is_multi_tenant:
        return JsonResponse({"error": "No tenant membership for this workspace"}, status=403)

    # Validate thread ownership so a user can't attach this turn to another
    # user's (or workspace's) thread. Return 404 not 403 to avoid leaking
    # thread existence. No except: thread_id is a valid UUID, so any lookup error
    # is a real DB failure and must 500 rather than skip the check.
    existing_thread = await Thread.objects.filter(id=thread_id).afirst()
    if existing_thread is not None and _is_foreign_thread(existing_thread, user, workspace):
        return _foreign_thread_response(existing_thread, user, workspace)

    # A RUNNING resume job means a resume ainvoke is writing this thread's checkpoint;
    # a concurrent live turn is a second unsynchronized writer (no CAS), so reject it.
    resume_in_flight = (
        existing_thread is not None
        and await ThreadJob.objects.filter(
            thread=existing_thread,
            state=ThreadJob.State.RUNNING,
        ).aexists()
    )
    if resume_in_flight:
        return JsonResponse(
            {
                "error": (
                    "A background response is still being generated for this "
                    "conversation. Please retry in a moment."
                )
            },
            status=409,
        )

    # The Thread row is the only authorization for this checkpointer key, so a
    # failed upsert must not fall through to the agent, and a row another user
    # created since the lookup above must be rejected, not joined.
    thread = await _upsert_thread(thread_id, user, user_content, workspace=workspace)
    if _is_foreign_thread(thread, user, workspace):
        return _foreign_thread_response(thread, user, workspace)

    # Reset inactivity TTL on user-initiated chat.
    await touch_workspace_schemas(workspace)

    try:
        mcp_tools = await get_mcp_tools()
    except Exception as e:
        error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
        logger.exception("Failed to load MCP tools [ref=%s]", error_ref)
        return JsonResponse({"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500)

    # Retry once with a fresh checkpointer on connection errors.
    try:
        checkpointer = await ensure_checkpointer()
        agent = await build_agent_graph(
            workspace=workspace,
            user=user,
            checkpointer=checkpointer,
            mcp_tools=mcp_tools,
            conversation_id=str(thread_id),
        )
    except Exception:
        try:
            logger.info("Retrying agent build with fresh checkpointer")
            checkpointer = await ensure_checkpointer(force_new=True)
            agent = await build_agent_graph(
                workspace=workspace,
                user=user,
                checkpointer=checkpointer,
                mcp_tools=mcp_tools,
                conversation_id=str(thread_id),
            )
        except Exception as e:
            error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
            logger.exception("Failed to build agent [ref=%s]", error_ref)
            return JsonResponse(
                {"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500
            )

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50,
    }

    # Repair any dangling tool_use calls from a previous interrupted turn.
    # If the user sent a new message while a tool was still in-flight, the
    # checkpoint will have an AIMessage with tool_calls but no matching
    # ToolMessages. Anthropic rejects such sequences with HTTP 400, so we
    # inject synthetic ToolMessages before appending the new HumanMessage.
    dangling_tool_results = await repair_dangling_tool_calls(agent, config)

    from langchain_core.messages import HumanMessage

    input_state = {
        "messages": [*dangling_tool_results, HumanMessage(content=user_content)],
        "workspace_id": str(workspace.id),
        "user_id": str(user.id),
        "thread_id": str(thread_id),
    }

    from apps.agents.tracing import get_langfuse_callback, langfuse_trace_context

    trace_metadata = {
        "workspace_id": str(workspace.id),
    }
    langfuse_handler = get_langfuse_callback(
        session_id=str(thread_id),
        user_id=str(user.id),
        metadata=trace_metadata,
    )
    if langfuse_handler is not None:
        config["callbacks"] = [langfuse_handler]

    trace_ctx = langfuse_trace_context(
        session_id=str(thread_id),
        user_id=str(user.id),
        metadata=trace_metadata,
    )

    async def _traced_stream():
        with trace_ctx:
            async for chunk in langgraph_to_ui_stream(agent, input_state, config):
                yield chunk

    response = StreamingHttpResponse(
        _traced_stream(),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
