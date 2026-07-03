"""
Chat views: streaming chat endpoint.

The chat endpoint is a raw async Django view (not DRF) because DRF
does not support async streaming responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from django.core.exceptions import ValidationError
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.helpers import (
    _resolve_workspace_and_membership,
    async_login_required,
    repair_dangling_tool_calls,
)
from apps.chat.models import Thread, ThreadJob
from apps.chat.rate_limiting import chat_rate_limit
from apps.chat.stream import langgraph_to_ui_stream
from apps.workspaces.services.workspace_service import touch_workspace_schemas

logger = logging.getLogger(__name__)


async def _upsert_thread(thread_id, user, title, *, workspace):
    """Create the Thread row if absent and bump updated_at on every turn.

    Ownership has already been validated by the caller — this helper only
    handles the (non-conflicting) upsert.

    The explicit ``updated_at`` in ``defaults`` is load-bearing: without it,
    ``aupdate_or_create`` on the existing-row path runs ``save(update_fields=set())``
    which skips ``auto_now`` and leaves ``Thread.updated_at`` frozen at the
    creation timestamp. That broke the sidebar's "newer than last_viewed"
    indicator and any ordering by ``-updated_at``.
    """
    await Thread.objects.aupdate_or_create(
        id=thread_id,
        defaults={"updated_at": timezone.now()},
        create_defaults={"user": user, "workspace": workspace, "title": title[:200]},
    )


MAX_MESSAGE_LENGTH = 10_000


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

    # Validate thread ownership so a user can't attach this turn to another
    # user's (or workspace's) thread. Return 404 not 403 to avoid leaking
    # thread existence.
    # SECURITY: catch ONLY the unmatchable-id cases (ValueError/ValidationError
    # from coercing a malformed UUID = "no such thread"). A broad except would
    # also swallow transient ORM errors, setting existing_thread=None and
    # SKIPPING the ownership check — fail-open. Let real errors propagate (→500)
    # so we never authorize access we couldn't verify.
    try:
        existing_thread = await Thread.objects.filter(id=thread_id).afirst()
    except (ValueError, ValidationError):
        existing_thread = None
    if existing_thread is not None and (
        existing_thread.user_id != user.pk or existing_thread.workspace_id != workspace.pk
    ):
        logger.warning(
            "Rejected chat POST to foreign thread: thread_id=%s requesting_user=%s "
            "owner_user=%s thread_workspace=%s requested_workspace=%s",
            thread_id,
            user.pk,
            existing_thread.user_id,
            existing_thread.workspace_id,
            workspace.pk,
        )
        return JsonResponse({"error": "Thread not found"}, status=404)

    # Don't stream a live turn while a background resume is mid-ainvoke against
    # this thread's checkpoint: two concurrent writers to one LangGraph thread can
    # interleave or drop superstep state, and there is no CAS at this seam. A
    # materialization ThreadJob is RUNNING only while its resume ainvoke is in
    # flight, so a RUNNING job here means a resume is writing — ask the user to
    # retry in a moment (arch #255, 06#9). (The reverse guard — a resume detecting
    # an in-flight live turn — needs a live-turn marker that does not exist yet;
    # tracked as follow-up.)
    resume_in_flight = await ThreadJob.objects.filter(
        thread_id=thread_id,
        state=ThreadJob.State.RUNNING,
    ).aexists()
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

    try:
        await _upsert_thread(
            thread_id,
            user,
            user_content,
            workspace=workspace,
        )
    except Exception:
        logger.warning("Failed to upsert thread %s", thread_id, exc_info=True)

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
        "user_role": "analyst",
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
