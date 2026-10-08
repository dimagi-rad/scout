"""Build and trace the agent a background chat turn runs, outside a live request."""

import contextlib
import logging

from django.conf import settings
from langchain_core.messages import AIMessage

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools
from apps.agents.tracing import langfuse_trace_context
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.stream import _write_terminal_message

try:
    from langfuse import Langfuse
except ImportError:  # langfuse is an optional observability dependency
    Langfuse = None

logger = logging.getLogger(__name__)


async def build_agent_for_resume(workspace, user, conversation_id=None):
    """Build the LangGraph agent for the resume task."""
    mcp_tools = await get_mcp_tools()
    checkpointer = await ensure_checkpointer()
    return await build_agent_graph(
        workspace=workspace,
        user=user,
        checkpointer=checkpointer,
        mcp_tools=mcp_tools,
        conversation_id=conversation_id,
    )


async def append_synthetic_message(thread, text: str) -> None:
    agent = await build_agent_for_resume(
        thread.workspace,
        thread.user,
        conversation_id=str(thread.id),
    )
    config = {"configurable": {"thread_id": str(thread.id)}}
    await _write_terminal_message(agent, config, AIMessage(content=text))


@contextlib.contextmanager
def resume_langfuse_span(
    *, thread_job_id: str, thread_id: str, user_id: str, workspace_id: str, status: str
):
    """Open the root Langfuse span around the resume ainvoke and propagate the
    thread's session/user onto every child observation, so resumed generations
    land in the same Langfuse session (and session cost) as the chat turn.

    Yields the span, or None when Langfuse is not configured so worker boots
    without LANGFUSE_* env vars stay quiet. Tracing is best-effort: an error
    entering or exiting it is logged, never raised, because the caller would
    otherwise mark a resume that already answered as agent_failed."""
    stack = contextlib.ExitStack()
    span = None
    secret_key = getattr(settings, "LANGFUSE_SECRET_KEY", "")
    public_key = getattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    base_url = getattr(settings, "LANGFUSE_BASE_URL", "")
    if Langfuse is not None and all([secret_key, public_key, base_url]):
        try:
            client = Langfuse(secret_key=secret_key, public_key=public_key, base_url=base_url)
            span = stack.enter_context(
                client.start_as_current_observation(
                    name="resume_thread_after_materialization",
                    input={
                        "thread_job_id": thread_job_id,
                        "thread_id": thread_id,
                        "status": status,
                    },
                )
            )
            stack.enter_context(
                langfuse_trace_context(
                    session_id=thread_id,
                    user_id=user_id,
                    metadata={"workspace_id": workspace_id},
                )
            )
        except Exception:
            logger.warning("resume: failed to open Langfuse span", exc_info=True)
            _close_langfuse_stack(stack, None)
            span = None
    try:
        yield span
    except BaseException as exc:
        _close_langfuse_stack(stack, exc)
        raise
    _close_langfuse_stack(stack, None)


def _close_langfuse_stack(stack: contextlib.ExitStack, exc: BaseException | None) -> None:
    try:
        if exc is None:
            stack.close()
        else:
            stack.__exit__(type(exc), exc, exc.__traceback__)
    except BaseException as close_exc:
        if close_exc is exc:
            return
        if not isinstance(close_exc, Exception):
            raise
        logger.warning("resume: failed to close Langfuse span", exc_info=True)


def final_message_content(result) -> object:
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return None
    return getattr(messages[-1], "content", None)
