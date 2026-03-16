"""
Chat views: streaming chat endpoint.

The chat endpoint is a raw async Django view (not DRF) because DRF
does not support async streaming responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid

from asgiref.sync import sync_to_async
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_protect

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    _resolve_workspace_and_membership,
    async_login_required,
)
from apps.chat.models import Thread
from apps.chat.stream import langgraph_to_ui_stream
from apps.projects.services.workspace_service import touch_workspace_schemas

logger = logging.getLogger(__name__)


@sync_to_async
def _upsert_thread(thread_id, user, title, *, workspace):
    """Create or update a Thread record.

    Explicitly validates ownership before upserting: if the thread_id already
    exists and belongs to a different user or workspace, the upsert is skipped
    with a warning rather than relying on a PK IntegrityError as a side-effect.
    auto_now on updated_at handles the timestamp on every save.
    """
    existing = Thread.objects.filter(id=thread_id).first()
    if existing is not None and (
        existing.user_id != user.pk or existing.workspace_id != workspace.pk
    ):
        logger.warning(
            "Thread %s belongs to a different user/workspace, skipping upsert",
            thread_id,
        )
        return
    # On create: set user, workspace, and title.
    # On update: no field changes needed — auto_now on updated_at handles the timestamp.
    Thread.objects.update_or_create(
        id=thread_id,
        create_defaults={"user": user, "workspace": workspace, "title": title[:200]},
    )


# ---------------------------------------------------------------------------
# Streaming chat endpoint
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = 10_000


@csrf_protect
@async_login_required
async def chat_view(request):
    """
    POST /api/chat/

    Accepts Vercel AI SDK useChat request format, returns a
    StreamingHttpResponse in the Data Stream Protocol.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    # Parse body
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    messages = body.get("messages", [])
    data = body.get("data", {})
    workspace_id = data.get("workspaceId") or body.get("workspaceId")
    thread_id = data.get("threadId") or body.get("threadId") or str(uuid.uuid4())

    if not messages:
        return JsonResponse({"error": "messages is required"}, status=400)
    if not workspace_id:
        return JsonResponse({"error": "workspaceId is required"}, status=400)

    # Get the last user message.
    # AI SDK v6 sends {parts: [{type:"text", text:"..."}]} instead of {content: "..."}.
    last_msg = messages[-1]
    user_content = last_msg.get("content", "")
    if not user_content:
        parts = last_msg.get("parts", [])
        user_content = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
    if not user_content or not user_content.strip():
        return JsonResponse({"error": "Empty message"}, status=400)
    if len(user_content) > MAX_MESSAGE_LENGTH:
        return JsonResponse(
            {"error": f"Message exceeds {MAX_MESSAGE_LENGTH} characters"}, status=400
        )

    # Resolve workspace and verify access. The multi-tenant flag is determined
    # in a single DB read inside _resolve_workspace_and_membership to avoid TOCTOU.
    workspace, tm, is_multi_tenant = await _resolve_workspace_and_membership(user, workspace_id)
    if workspace is None:
        return JsonResponse({"error": "Workspace not found or access denied"}, status=403)

    if tm is None and not is_multi_tenant:
        return JsonResponse({"error": "No tenant membership for this workspace"}, status=403)

    # Record thread metadata (fire-and-forget on error)
    try:
        await _upsert_thread(
            thread_id,
            user,
            user_content,
            workspace=workspace,
        )
    except Exception:
        logger.warning("Failed to upsert thread %s", thread_id, exc_info=True)

    # Touch the schema to reset inactivity TTL on user-initiated chat.
    await touch_workspace_schemas(workspace)

    # Load MCP tools; attach progress callback for run_materialization updates.
    progress_queue: asyncio.Queue = asyncio.Queue()

    async def _on_mcp_progress(progress, total, message, context) -> None:
        if message is not None:
            await progress_queue.put(
                {
                    "current": int(progress),
                    "total": int(total) if total else 0,
                    "message": message,
                }
            )

    try:
        mcp_tools = await get_mcp_tools(on_progress=_on_mcp_progress)
    except Exception as e:
        error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
        logger.exception("Failed to load MCP tools [ref=%s]", error_ref)
        return JsonResponse({"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500)

    # Retrieve user's OAuth tokens for materialization
    oauth_tokens = await get_user_oauth_tokens(user)

    # Build agent (retry once with fresh checkpointer on connection errors)
    try:
        checkpointer = await ensure_checkpointer()
        agent = await build_agent_graph(
            workspace=workspace,
            user=user,
            checkpointer=checkpointer,
            mcp_tools=mcp_tools,
            oauth_tokens=oauth_tokens,
        )
    except Exception:
        # Connection may have gone stale -- force a new checkpointer and retry
        try:
            logger.info("Retrying agent build with fresh checkpointer")
            checkpointer = await ensure_checkpointer(force_new=True)
            agent = await build_agent_graph(
                workspace=workspace,
                user=user,
                checkpointer=checkpointer,
                mcp_tools=mcp_tools,
                oauth_tokens=oauth_tokens,
            )
        except Exception as e:
            error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
            logger.exception("Failed to build agent [ref=%s]", error_ref)
            return JsonResponse(
                {"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500
            )

    # Build LangGraph input state
    from langchain_core.messages import HumanMessage

    input_state = {
        "messages": [HumanMessage(content=user_content)],
        "workspace_id": str(workspace.id),
        "user_id": str(user.id),
        "user_role": "analyst",
        "needs_correction": False,
        "retry_count": 0,
        "correction_context": {},
    }
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50,
        "oauth_tokens": oauth_tokens,
    }

    # Attach Langfuse tracing callback if configured
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
            async for chunk in langgraph_to_ui_stream(
                agent, input_state, config, progress_queue=progress_queue
            ):
                yield chunk

    # Return streaming response (SSE for AI SDK v6 DefaultChatTransport)
    response = StreamingHttpResponse(
        _traced_stream(),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
