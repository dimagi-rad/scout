"""
Chat views: streaming chat endpoint.

The chat endpoint is a raw async Django view (not DRF) because DRF
does not support async streaming responses.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid

from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from langchain_core.messages import HumanMessage

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools
from apps.agents.tracing import get_langfuse_callback, langfuse_trace_context
from apps.chat import pending_requests
from apps.chat.checkpointer import athread_has_checkpoint, ensure_checkpointer
from apps.chat.constants import MAX_MESSAGE_LENGTH
from apps.chat.helpers import (
    _resolve_chat_access,
    async_login_required,
    repair_dangling_tool_calls,
)
from apps.chat.models import Thread
from apps.chat.rate_limiting import chat_rate_limit
from apps.chat.stream import _sse, langgraph_to_ui_stream
from apps.chat.tasks import aschedule_thread_title
from apps.chat.titles import afill_turn_title, short_thread_title
from apps.chat.turn_lease import TurnLease, aacquire_turn_lease
from apps.common.capacity import BUSY_ERROR, RETRY_AFTER_SECONDS, classify_capacity_error
from apps.common.http import parse_json_object
from apps.workspaces.access import access_denied_body, role_satisfies
from apps.workspaces.models import WorkspaceRole
from apps.workspaces.services.load_activity import (
    athread_awaits_load,
    aworkspace_serves_nothing,
)
from apps.workspaces.services.thread_job_dispatch import (
    astart_chat_load,
    astart_chat_semantic_rebuild,
)
from apps.workspaces.services.workspace_service import touch_workspace_schemas

logger = logging.getLogger(__name__)


class ForeignThreadError(Exception):
    def __init__(self, thread: Thread):
        super().__init__(f"Thread {thread.id} belongs to another user or workspace")
        self.thread = thread


async def _upsert_thread(thread_id, user, history_title: str = "", *, workspace) -> Thread:
    """Create the Thread row if absent and bump updated_at on every turn.

    Raises ``ForeignThreadError``, without touching the row, when it belongs to
    another user or workspace — including a row created by someone else after the
    caller's lookup missed. Raising (rather than returning a verdict) means a
    caller that forgets to handle it still fails closed.

    The explicit ``updated_at`` bump is load-bearing: without it the sidebar's
    "newer than last_viewed" indicator and ``-updated_at`` ordering freeze at
    the creation timestamp.
    """
    thread, created = await Thread.objects.aget_or_create(
        id=thread_id,
        defaults={
            "user": user,
            "workspace": workspace,
            "title": short_thread_title(history_title),
            "title_is_custom": False,
            "title_source": Thread.TitleSource.FIRST_MESSAGE,
        },
    )
    if _is_foreign_thread(thread, user, workspace):
        raise ForeignThreadError(thread)
    if not created:
        await Thread.objects.filter(pk=thread.pk).aupdate(updated_at=timezone.now())
        await afill_turn_title(thread, history_title)
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


TURN_LEASE_WAIT_SECONDS = 2
THREAD_BUSY_MESSAGE = (
    "A response is still being generated for this conversation. Please retry in a moment."
)


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
            {
                "error": f"Message exceeds {MAX_MESSAGE_LENGTH:,} characters. Shorten it and send again.",
                "reason": "message_too_long",
            },
            status=400,
        )

    # Resolve workspace and verify access. The multi-tenant flag is determined
    # in a single DB read inside _resolve_chat_access to avoid TOCTOU.
    access, tm, is_multi_tenant = await _resolve_chat_access(user, workspace_id)
    workspace = access.workspace
    if workspace is None:
        # The chat UI offers Retry unless a reason says resending cannot help.
        body = {"reason": "access_denied", **access_denied_body(access)}
        return JsonResponse(body, status=403)

    if tm is None and not is_multi_tenant:
        return JsonResponse(
            {"error": "No tenant membership for this workspace", "reason": "access_denied"},
            status=403,
        )

    # Validate thread ownership so a user can't attach this turn to another
    # user's (or workspace's) thread. Return 404 not 403 to avoid leaking
    # thread existence. No except: thread_id is a valid UUID, so any lookup error
    # is a real DB failure and must 500 rather than skip the check.
    existing_thread = await Thread.objects.filter(id=thread_id).afirst()
    if existing_thread is not None and _is_foreign_thread(existing_thread, user, workspace):
        return _foreign_thread_response(existing_thread, user, workspace)
    if existing_thread is None and await athread_has_checkpoint(thread_id):
        logger.warning(
            "Rejected chat POST reusing a deleted thread's id: thread_id=%s requesting_user=%s",
            thread_id,
            user.pk,
        )
        return JsonResponse({"error": "Thread not found"}, status=404)

    # The Thread row is the only authorization for this checkpointer key, so a
    # failed upsert must propagate rather than fall through to the agent.
    try:
        thread = await _upsert_thread(thread_id, user, user_content, workspace=workspace)
    except ForeignThreadError as e:
        return _foreign_thread_response(e.thread, user, workspace)

    pending_version, err = _pending_request_version(data)
    if err:
        return err

    # Before the hold decision, so the message that starts a chat's first load is held too.
    if (
        role_satisfies(access.membership.role, WorkspaceRole.READ_WRITE)
        and await astart_chat_load(workspace=workspace, user=user, thread_id=thread_id) is None
    ):
        await astart_chat_semantic_rebuild(workspace=workspace, user=user)

    # A version means the client is sending the held request itself (its "Send now").
    if pending_version is None:
        try:
            held = await _hold_while_loading(workspace, thread_id, messages[-1], user_content)
        except pending_requests.PendingRequestTooLong:
            return _request_too_long_response()
        if held is not None:
            return _held_response(held)

    # A brief wait covers Stop-then-resend: the stopped turn releases the lease
    # only after persisting its partial reply.
    lease = await aacquire_turn_lease(thread_id, wait_seconds=TURN_LEASE_WAIT_SECONDS)
    if lease is None:
        return _thread_busy_response()
    try:
        async with lease.kept_alive():
            response = await _start_turn(
                lease,
                user=user,
                workspace=workspace,
                access=access,
                thread=thread,
                user_content=user_content,
                pending_version=pending_version,
            )
    except asyncio.CancelledError:
        await lease.release()
        if not lease.lost:
            raise
        # The heartbeat cancelled us because another run took the thread; that is
        # a busy thread for the caller, not a dropped connection.
        asyncio.current_task().uncancel()
        return _thread_busy_response()
    except BaseException:
        await lease.release()
        raise
    if lease.lost or not isinstance(response, StreamingHttpResponse):
        held_claim = getattr(response, "held_claim", None)
        if held_claim is not None:
            await pending_requests.arelease(held_claim)
        await lease.release()
        if lease.lost:
            return _thread_busy_response()
    return response


def _pending_request_version(data: dict) -> tuple[int | None, JsonResponse | None]:
    version = data.get("pendingRequestVersion")
    if version is None:
        return None, None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None, JsonResponse(
            {"error": "pendingRequestVersion must be a positive integer"}, status=400
        )
    return version, None


async def _hold_while_loading(workspace, thread_id: str, message: dict, text: str) -> dict | None:
    """Hold the message for the load this chat awaits, or None to answer it now.

    Only while the workspace serves no data: a refresh over served data answers
    from what is there. A chat with no load to wait on gets a normal turn, whose
    agent explains why nothing can load.
    """
    if not await athread_awaits_load(thread_id):
        return None
    if not await aworkspace_serves_nothing(workspace.id):
        return None
    part_id = message.get("id")
    if (
        not isinstance(part_id, str)
        or not part_id
        or len(part_id) > pending_requests.MAX_PART_ID_LENGTH
    ):
        part_id = str(uuid.uuid4())
    return await pending_requests.ahold_message(thread_id, part_id=part_id, text=text)


def _held_response(pending: dict) -> StreamingHttpResponse:
    """A turn with no model call: the message joined the request held for the load."""

    async def body():
        yield _sse({"type": "start"})
        # Transient, so the SDK hands it to onData and adds no assistant message.
        yield _sse({"type": "data-pending-request", "data": pending, "transient": True})
        yield _sse({"type": "finish", "finishReason": "stop"})

    response = StreamingHttpResponse(body(), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _request_too_long_response() -> JsonResponse:
    return JsonResponse(
        {
            "error": pending_requests.REQUEST_TOO_LONG_MESSAGE,
            "reason": "pending_request_too_long",
        },
        status=400,
    )


def _pending_conflict_response(reason: str) -> JsonResponse:
    return JsonResponse({"error": "pending_request_conflict", "reason": reason}, status=409)


def _thread_busy_response() -> JsonResponse:
    # The "busy" error code makes the chat UI back off and resend, as it does for a
    # capacity 503: the turn was refused before anything touched the checkpoint.
    response = JsonResponse(
        {"error": BUSY_ERROR, "reason": "thread_busy", "message": THREAD_BUSY_MESSAGE},
        status=409,
    )
    response["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response


async def _start_turn(
    lease: TurnLease,
    *,
    user,
    workspace,
    access,
    thread: Thread,
    user_content: str,
    pending_version: int | None = None,
):
    """Build the agent and return the turn's stream, which owns ``lease`` from here."""
    thread_id = str(thread.id)
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
    except Exception as first_error:
        # A pool that is full stays full: a rebuild would only wait out another
        # PoolTimeout. The capacity middleware answers with a retryable 503.
        capacity = classify_capacity_error(first_error)
        if capacity is not None:
            raise capacity from first_error
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
            capacity = classify_capacity_error(e)
            if capacity is not None:
                raise capacity from e
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

    # Last before the response exists, so nothing between can strand the claim; an
    # unsent response returns it to waiting on close.
    claimed, human_message = await _claim_held_request(
        thread_id, lease, user_content=user_content, pending_version=pending_version
    )
    if isinstance(human_message, JsonResponse):
        return human_message
    input_state = {
        "messages": [*dangling_tool_results, human_message],
        "workspace_id": str(workspace.id),
        "user_id": str(user.id),
        "thread_id": str(thread_id),
    }

    response = _TurnStreamingResponse(lease, held_claim=claimed)

    async def _traced_stream():
        response.turn_started = True
        # aclosing: nothing else closes the inner stream on disconnect, and its
        # cleanup (stopping the run, saving the partial reply) must finish before
        # the lease is released and another turn can take the thread.
        async with lease.held():
            try:
                with trace_ctx:
                    async with contextlib.aclosing(
                        langgraph_to_ui_stream(
                            agent,
                            input_state,
                            config,
                            owns_thread=lambda: not lease.lost,
                            on_success=lambda: aschedule_thread_title(thread),
                        )
                    ) as stream:
                        async for chunk in stream:
                            yield chunk
            finally:
                if claimed is not None:
                    await pending_requests.asettle(claimed)

    response.streaming_content = _traced_stream()
    return response


async def _claim_held_request(
    thread_id: str, lease: TurnLease, *, user_content: str, pending_version: int | None
) -> tuple[pending_requests.ClaimedRequest | None, HumanMessage | JsonResponse]:
    """The turn's message, carrying any request still held for the thread.

    A held request goes out with the next turn, so its text is never stranded
    behind a load that will not resume it.
    """
    try:
        claimed = await pending_requests.aclaim(thread_id, lease.token)
    except Exception:
        logger.exception("Could not claim the held request of thread %s", thread_id)
        if pending_version is not None:
            return None, _pending_conflict_response("unavailable")
        # The request stays held and is offered again; this turn need not fail with it.
        return None, HumanMessage(content=user_content)
    if claimed is None:
        if pending_version is not None:
            return None, _pending_conflict_response("gone")
        return None, HumanMessage(content=user_content)
    if pending_version is not None and pending_version != claimed.version:
        await pending_requests.arelease(claimed)
        return None, _pending_conflict_response("version")
    # The client sends a held request it showed whole; otherwise the held text leads.
    content = (
        user_content
        if pending_version is not None
        else f"{claimed.text}{pending_requests.PART_SEPARATOR}{user_content}"
    )
    if len(content) > MAX_MESSAGE_LENGTH:
        await pending_requests.arelease(claimed)
        return None, _request_too_long_response()
    return claimed, HumanMessage(content=content, id=claimed.message_id)


class _TurnStreamingResponse(StreamingHttpResponse):
    """The turn's SSE response; frees the thread if the server closes it unsent.

    Once the body has started, the stream itself releases the lease after its
    cleanup, so ``close`` must leave a started turn alone.
    """

    def __init__(
        self, lease: TurnLease, *, held_claim: pending_requests.ClaimedRequest | None = None
    ):
        super().__init__(content_type="text/event-stream; charset=utf-8")
        self["Cache-Control"] = "no-cache"
        self["X-Accel-Buffering"] = "no"
        self.lease = lease
        self.held_claim = held_claim
        self.turn_started = False

    def close(self):
        try:
            if not self.turn_started:
                # Before the lease, so no next holder sees this claim as stale.
                if self.held_claim is not None:
                    pending_requests.release_sync(self.held_claim)
                self.lease.release_sync()
        except Exception:
            logger.warning(
                "Could not release the turn lease of unsent response on thread %s",
                self.lease.thread_id,
                exc_info=True,
            )
        finally:
            super().close()
