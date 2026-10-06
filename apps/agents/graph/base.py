"""
LangGraph agent graph builder for the Scout data agent platform.

`build_agent_graph` assembles a loop (agent -> tools -> agent) that relies on
the LLM to self-correct from error ToolMessages; a recursion limit bounds runaway
loops and a panic-loop detector escalates after repeated schema errors.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.prompt_context import (
    PROMPT_CACHE_CONTROL,
    _build_cached_system_message,
    _build_system_prompt,
)
from apps.agents.graph.run_policy import (
    ESCALATION_MESSAGE,
    ESCALATION_METADATA_KEY,
    ESCALATION_TRIGGER_COUNT,
    HEADLESS_ESCALATION_MESSAGE,
    MODEL_STOPPED_MESSAGES,
    READ_ONLY_ESCALATION_MESSAGE,
    READ_ONLY_SEMANTIC_ESCALATION_MESSAGE,
    SEMANTIC_ESCALATION_MESSAGE,
    _schema_escalation_message,
    _should_escalate,
    _workspace_access_denial,
)
from apps.agents.graph.state import (
    TRUNCATED_TOOL_CALLS_NODE,
    AgentState,
    all_tool_calls,
    prune_messages,
    reject_truncated_tool_calls,
    truncated_retries_exhausted,
    truncated_tool_calls,
    unfinished_turn_reason,
)
from apps.agents.graph.tool_binding import (
    INJECTED_TOOL_PARAMS,
    _build_tools,
    _llm_tool_schemas,
    _make_injecting_tool_node,
)
from apps.agents.llm_request import MAIN_AGENT_EFFORT, chat_model_kwargs
from apps.memory.services import apersonal_memory_prompt, has_personal_memory
from apps.semantic.services.date_context import agent_date_context
from apps.workspaces.access import aresolve_workspace_access_ex
from apps.workspaces.models import WorkspaceRole

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

# The node whose model call writes the answer; resume streaming tails its tokens.
AGENT_NODE = "agent"

# Adaptive thinking counts toward max_tokens, so 4096 could cut a turn off
# mid-answer or mid-tool-call. Every call streams (see chat_model_kwargs), so the
# SDK's 21,333-token guard for non-streamed requests does not apply.
DEFAULT_MAX_TOKENS = 16_000

# Graph nodes that end a turn with a fixed AIMessage instead of an LLM call, so
# the chat stream must emit their text itself.
FIXED_MESSAGE_NODES = frozenset({"escalate", "model_stopped"})


async def build_agent_graph(
    workspace: Workspace,
    user: User | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_tools: list | None = None,
    conversation_id: str | None = None,
    *,
    interactive: bool = True,
    job_id: int | None = None,
):
    """
    Build a LangGraph agent graph for a workspace.

    Args:
        workspace: The Workspace model instance.
        user: Optional User model instance.
        checkpointer: Optional LangGraph checkpointer for conversation persistence.
        mcp_tools: List of MCP tools to include.
        conversation_id: Optional thread/conversation id. Threaded through to the
            artifact tools so chat-created artifacts record their originating
            conversation (so the thread's artifact list can find them).
        interactive: Whether this graph serves an interactive chat turn (the
            default). Interactive runs own a real Thread + persistent checkpointer
            and use the fire-and-ack MCP ``run_materialization`` + async resume.
            Headless runs (``interactive=False``, e.g. recipe execution) have no
            Thread/checkpointer/resume path, so they get a *blocking*
            ``run_materialization`` tool instead and matching prompt guidance.
        job_id: Enclosing Procrastinate job id, passed to the headless
            materialization tool for MaterializationRun traceability. Ignored in
            interactive mode.
    """
    logger.info("Building agent graph for workspace %s (interactive=%s)", workspace.id, interactive)

    write_access = None
    if user is not None and getattr(user, "is_authenticated", False):
        write_access = await aresolve_workspace_access_ex(
            user, workspace.id, minimum_role=WorkspaceRole.READ_WRITE
        )
    write_capable = bool(write_access is not None and write_access.granted)
    canvas_write = bool(interactive and conversation_id and write_capable)

    # --- Build tools ---
    tools = _build_tools(
        workspace,
        user,
        mcp_tools or [],
        conversation_id=conversation_id,
        interactive=interactive,
        job_id=job_id,
        canvas_write=canvas_write,
        write_capable=write_capable,
    )
    logger.debug("Created %d tools for workspace %s", len(tools), workspace.id)

    injections = {
        "workspace_id": "workspace_id",
        "user_id": "user_id",
        "thread_id": "thread_id",
    }
    # tool_call_id is injected per-call (from the tool_call's own id), not from
    # state, so it can't live in `injections`. INJECTED_TOOL_PARAMS is the single
    # source of truth for the hidden set (also redacts tool input in the SSE stream).
    hidden_params = list(INJECTED_TOOL_PARAMS)

    # Opus 4.7+ removed sampling params (temperature/top_p/top_k); sending any 400s.
    llm = ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
        **chat_model_kwargs(MAIN_AGENT_EFFORT),
    )
    llm_tool_schemas = _llm_tool_schemas(tools, hidden_params=hidden_params)
    llm_with_tools = llm.bind_tools(llm_tool_schemas)

    stable_prompt, volatile_prompt = await _build_system_prompt(
        workspace,
        user,
        interactive=interactive,
        canvas_write=canvas_write,
        write_capable=write_capable,
        conversation_id=conversation_id,
    )
    volatile_prompt += agent_date_context()
    # Read every turn rather than cached, so a memory edit applies on the next message.
    personal_memory = (
        await apersonal_memory_prompt(user) if interactive and has_personal_memory(user) else ""
    )
    logger.debug(
        "System prompt assembled: %d stable + %d volatile chars for workspace %s",
        len(stable_prompt),
        len(volatile_prompt),
        workspace.id,
    )

    base_tool_node = ToolNode(tools)
    tool_node = _make_injecting_tool_node(base_tool_node, injections)

    async def agent_node(state: AgentState) -> dict[str, Any]:
        """Prepend the system prompt and invoke the LLM.

        ``prune_messages`` bounds replayed history so per-turn input tokens don't
        grow without limit as a thread ages — recursion_limit caps tool iterations,
        not conversation length, and large query results were otherwise replayed
        verbatim every call (arch #254, finding 01#3). Cache breakpoints bill the
        static prefix and bounded history at cache-read rates (02#3).
        """
        state_messages = list(state["messages"])
        # Drop prior system messages to avoid duplicates across cycles
        state_messages = [m for m in state_messages if not isinstance(m, SystemMessage)]
        state_messages = prune_messages(state_messages)

        # Ensure every AIMessage with tool_calls is followed by matching
        # ToolMessages, injecting synthetic ones if not, so Anthropic never gets
        # an invalid tool_use/tool_result sequence.
        answered_ids: set[str] = {
            m.tool_call_id for m in state_messages if isinstance(m, ToolMessage) and m.tool_call_id
        }
        repaired: list = []
        for msg in state_messages:
            repaired.append(msg)
            if isinstance(msg, AIMessage):
                for tc in all_tool_calls(msg):
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in answered_ids:
                        logger.warning(
                            "agent_node: found dangling tool_call_id=%s tool_name=%s — "
                            "injecting synthetic tool_result to satisfy Anthropic protocol",
                            tc_id,
                            tc.get("name") or "unknown",
                        )
                        repaired.append(
                            ToolMessage(
                                content=(
                                    "Tool call was interrupted — the user sent a new message "
                                    "before this tool completed."
                                ),
                                tool_call_id=tc_id,
                                name=tc.get("name") or "unknown",
                            )
                        )
                        answered_ids.add(tc_id)

        messages = [
            _build_cached_system_message(stable_prompt, volatile_prompt, personal_memory),
            *repaired,
        ]
        # cache_control lands on the last eligible message block, caching the
        # (pruned) conversation-history prefix (arch #254, 02#3).
        response = await llm_with_tools.ainvoke(messages, cache_control=PROMPT_CACHE_CONTROL)
        return {"messages": [response]}

    def should_continue(
        state: AgentState,
    ) -> Literal["tools", "truncated_tool_calls", "model_stopped", "__end__"]:
        """Route to tools on tool calls, to model_stopped on an unanswered turn, else end."""
        messages = state.get("messages", [])
        if not messages:
            return END

        last_message = messages[-1]
        if truncated_tool_calls(last_message):
            return TRUNCATED_TOOL_CALLS_NODE
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        if unfinished_turn_reason(last_message) is not None:
            return "model_stopped"

        return END

    def model_stopped_node(state: AgentState) -> dict[str, Any]:
        """Terminal node: tell the user the model stopped instead of leaving a blank reply."""
        reason = unfinished_turn_reason(state["messages"][-1]) or "empty"
        logger.warning(
            "agent graph: model turn ended without an answer (reason=%s, workspace=%s)",
            reason,
            workspace.id,
        )
        return {
            "messages": [
                AIMessage(
                    content=MODEL_STOPPED_MESSAGES[reason],
                    response_metadata={ESCALATION_METADATA_KEY: f"model_{reason}"},
                )
            ]
        }

    def post_tools_router(state: AgentState) -> Literal["agent", "escalate"]:
        """Route post-tools: escalate if the agent is in a panic loop, else agent.

        See ``_should_escalate`` for the loop-detection rule. The escalation
        node ends the turn with a fixed message — no further tool calls.
        """
        if _workspace_access_denial(state.get("messages", [])) is not None:
            return "escalate"
        if _should_escalate(state.get("messages", [])):
            logger.warning(
                "agent graph: routing to escalation node after %d consecutive "
                "tool errors (workspace=%s)",
                ESCALATION_TRIGGER_COUNT,
                workspace.id,
            )
            return "escalate"
        return "agent"

    def escalation_node(state: AgentState) -> dict[str, Any]:
        """Terminal node that emits a fixed escalation message and ends the turn."""
        denial = _workspace_access_denial(state.get("messages", []))
        if denial is not None:
            message = denial
        else:
            message = _schema_escalation_message(
                state.get("messages", []), write_capable=write_capable, interactive=interactive
            )
        reason = "workspace_access_denied" if denial is not None else "schema_errors"
        return {
            "messages": [
                AIMessage(content=message, response_metadata={ESCALATION_METADATA_KEY: reason})
            ]
        }

    graph = StateGraph(AgentState)

    graph.add_node(AGENT_NODE, agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("escalate", escalation_node)
    graph.add_node("model_stopped", model_stopped_node)
    graph.add_node(TRUNCATED_TOOL_CALLS_NODE, reject_truncated_tool_calls)

    graph.set_entry_point("agent")

    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            TRUNCATED_TOOL_CALLS_NODE: TRUNCATED_TOOL_CALLS_NODE,
            "model_stopped": "model_stopped",
            END: END,
        },
    )

    # tools -> agent (normal) or -> escalate (panic loop, terminal)
    graph.add_conditional_edges(
        "tools",
        post_tools_router,
        {
            "agent": "agent",
            "escalate": "escalate",
        },
    )
    graph.add_edge("escalate", END)
    graph.add_edge("model_stopped", END)
    graph.add_conditional_edges(
        TRUNCATED_TOOL_CALLS_NODE,
        lambda state: (
            "model_stopped" if truncated_retries_exhausted(state["messages"]) else "agent"
        ),
        {"agent": "agent", "model_stopped": "model_stopped"},
    )

    compiled = graph.compile(checkpointer=checkpointer)

    logger.info(
        "Agent graph compiled for workspace %s (checkpointer: %s)",
        workspace.id,
        type(checkpointer).__name__ if checkpointer else "None",
    )

    return compiled


__all__ = [
    "ESCALATION_MESSAGE",
    "ESCALATION_METADATA_KEY",
    "ESCALATION_TRIGGER_COUNT",
    "FIXED_MESSAGE_NODES",
    "HEADLESS_ESCALATION_MESSAGE",
    "INJECTED_TOOL_PARAMS",
    "READ_ONLY_ESCALATION_MESSAGE",
    "READ_ONLY_SEMANTIC_ESCALATION_MESSAGE",
    "SEMANTIC_ESCALATION_MESSAGE",
    "_should_escalate",
    "build_agent_graph",
]
