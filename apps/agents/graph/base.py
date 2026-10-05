"""
LangGraph agent graph builder for the Scout data agent platform.

`build_agent_graph` assembles a loop (agent -> tools -> agent) that relies on
the LLM to self-correct from error ToolMessages; a recursion limit bounds runaway
loops and a panic-loop detector escalates after repeated schema errors.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
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
    _artifact_manager_task_is_missing,
    _schema_escalation_message,
    _should_escalate,
    _synthesize_artifact_manager_task,
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
from apps.agents.llm_request import MAIN_AGENT_EFFORT, chat_model_kwargs
from apps.agents.subagents.events import (
    HUMAN_TURN_PARAM,
    SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    SUBAGENT_TOOL_NAMES,
    reset_subagent_event_queue,
    set_subagent_event_queue,
)
from apps.agents.tool_results import compact_tool_results
from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.materialization_tool import create_materialization_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.semantic.services.date_context import agent_date_context
from apps.workspaces.access import aresolve_workspace_access_ex
from apps.workspaces.models import WorkspaceRole

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

# MCP tools that require a context ID (tenant_id) injected from state
MCP_TOOL_NAMES = frozenset(
    {
        "list_tables",
        "describe_table",
        "query",
        "get_metadata",
        "list_workspaces",
        "list_datasets",
        "semantic_catalog",
        "describe_dataset",
        "semantic_query",
        "run_materialization",
        "get_schema_status",
        "get_lineage",
        # workspace_id is injected into these so an LLM-supplied run_id is scoped
        # to the calling workspace (arch #253, 01#6).
        "get_materialization_status",
        "cancel_materialization",
    }
)

LOCAL_CONTEXT_TOOL_NAMES = frozenset({"artifact_manager", "canvas_manager"})
ARTIFACT_READ_TOOL_NAMES = frozenset({"artifact_graph_overview", "get_artifact_semantic_queries"})

AGENT_WRITE_MCP_TOOLS = frozenset({"run_materialization", "cancel_materialization"})

# Context params the graph injects into every MCP tool call server-side. They
# are hidden from the LLM-facing tool schema (so the model never sets them) and
# must also be stripped from any tool input surfaced to the UI (they carry
# internal ids, not arguments the user typed). ``tool_call_id`` is injected
# per-call from the LangChain tool_call's own id; the rest come from agent
# state. Kept here as the single source of truth so the SSE stream's
# input-redaction stays in lockstep with what the graph injects.
INJECTED_TOOL_PARAMS = frozenset(
    {
        "workspace_id",
        "user_id",
        "thread_id",
        "tool_call_id",
        SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
        HUMAN_TURN_PARAM,
    }
)


def human_turn_count(messages: list) -> int:
    """Messages the user actually typed; resume notices are synthetic HumanMessages."""
    return sum(
        1
        for message in messages
        if isinstance(message, HumanMessage)
        and not str(message.content).startswith(SYSTEM_RESUME_MARKER)
    )


# The node whose model call writes the answer; resume streaming tails its tokens.
AGENT_NODE = "agent"

# Adaptive thinking counts toward max_tokens, so 4096 could cut a turn off
# mid-answer or mid-tool-call. Every call streams (see chat_model_kwargs), so the
# SDK's 21,333-token guard for non-streamed requests does not apply.
DEFAULT_MAX_TOKENS = 16_000

# Graph nodes that end a turn with a fixed AIMessage instead of an LLM call, so
# the chat stream must emit their text itself.
FIXED_MESSAGE_NODES = frozenset({"escalate", "model_stopped"})


def _llm_tool_schemas(tools: list, hidden_params: list[str]) -> list:
    """Build LLM tool definitions with the injected context-ID params omitted from
    the schema, so the LLM can't supply (and hallucinate) values that are injected
    from state. Non-MCP tools are returned unchanged.
    """
    hidden = set(hidden_params)
    result: list = []
    for tool in tools:
        schema = tool.get_input_schema().model_json_schema()
        props = schema.get("properties", {})
        to_hide = hidden & set(props)

        if not to_hide:
            result.append(tool)
            continue

        if tool.name not in MCP_TOOL_NAMES and tool.name not in LOCAL_CONTEXT_TOOL_NAMES:
            result.append(tool)
            continue

        # Build a trimmed schema dict for bind_tools
        trimmed_props = {k: v for k, v in props.items() if k not in to_hide}
        trimmed_required = [r for r in schema.get("required", []) if r not in to_hide]
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": {
                        "type": "object",
                        "properties": trimmed_props,
                        "required": trimmed_required,
                    },
                },
            }
        )
    return result


def _make_injecting_tool_node(
    base_tool_node: ToolNode,
    injections: dict[str, str],
) -> Any:
    """Wrap a ToolNode so MCP tool calls get context IDs from agent state.

    Copies the last AI message and injects state values into every MCP tool
    call's args before execution. ``injections`` maps tool-arg-name →
    state-field-name, so the MCP server always gets correct IDs regardless of
    what the LLM generated.
    """

    async def injecting_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        messages = list(state["messages"])
        last_msg = messages[-1]
        event_queue = None
        if isinstance(config, dict):
            event_queue = (config.get("configurable") or {}).get(SUBAGENT_EVENT_QUEUE_CONFIG_KEY)

        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            modified_msg = copy.copy(last_msg)
            persistable_msg = copy.copy(last_msg)
            modified_calls = []
            persistable_calls = []
            persistable_changed = False
            for tc in last_msg.tool_calls:
                tc_id = tc.get("id") or ""
                if tc["name"] in MCP_TOOL_NAMES:
                    extra = {k: state.get(v, "") for k, v in injections.items()}
                    if not tc_id:
                        logger.warning(
                            "MCP tool call '%s' has no id; tool_call_id will be empty — "
                            "background-job attribution will fail",
                            tc["name"],
                        )
                    extra["tool_call_id"] = tc_id
                    tc = {**tc, "args": {**tc["args"], **extra}}
                    persistable_tc = copy.deepcopy(tc)
                    persistable_tc["args"] = {
                        k: v
                        for k, v in persistable_tc.get("args", {}).items()
                        if k not in INJECTED_TOOL_PARAMS
                    }
                elif tc["name"] in LOCAL_CONTEXT_TOOL_NAMES:
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    if tc["name"] == "artifact_manager" and _artifact_manager_task_is_missing(args):
                        task = _synthesize_artifact_manager_task(messages)
                        args = {**args, "task": task}
                        logger.warning(
                            "artifact_manager tool call had empty task; synthesized "
                            "fallback task from latest user request (tool_call_id=%s)",
                            tc_id,
                        )
                        persistable_changed = True
                    persistable_tc = {**tc, "args": dict(args)}
                    extra = {"tool_call_id": tc_id}
                    if tc["name"] in SUBAGENT_TOOL_NAMES:
                        extra[SUBAGENT_EVENT_QUEUE_CONFIG_KEY] = event_queue
                    if tc["name"] == "canvas_manager":
                        extra[HUMAN_TURN_PARAM] = human_turn_count(messages)
                    tc = {**tc, "args": {**args, **extra}}
                else:
                    persistable_tc = tc
                modified_calls.append(tc)
                persistable_calls.append(persistable_tc)
            modified_msg.tool_calls = modified_calls
            if persistable_changed:
                persistable_msg.tool_calls = persistable_calls
            messages = [*messages[:-1], modified_msg]

        token = set_subagent_event_queue(event_queue)
        try:
            result = compact_tool_results(
                await base_tool_node.ainvoke({"messages": messages}, config=config),
                MCP_TOOL_NAMES,
            )
            if (
                "persistable_msg" in locals()
                and persistable_changed
                and getattr(persistable_msg, "id", None)
                and isinstance(result, dict)
            ):
                result_messages = list(result.get("messages", []))
                return {**result, "messages": [persistable_msg, *result_messages]}
            return result
        finally:
            reset_subagent_event_queue(token)

    injecting_node.__annotations__["config"] = RunnableConfig | None
    return injecting_node


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
            _build_cached_system_message(stable_prompt, volatile_prompt),
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


def _build_tools(
    workspace: Workspace,
    user: User | None,
    mcp_tools: list,
    conversation_id: str | None = None,
    interactive: bool = True,
    job_id: int | None = None,
    canvas_write: bool = False,
    write_capable: bool = False,
) -> list:
    """Build the tool list: MCP data tools plus local artifact/recipe/learning
    tools, and a blocking materialization tool in headless mode.
    """
    # Write-capable MCP tools are dropped for read-only roles. In headless mode
    # also drop the interactive fire-and-ack
    # ``run_materialization``: it requires a real chat Thread + checkpointer +
    # async resume that a headless run does not have. It is replaced below by the
    # blocking materialize tool, which runs the pipeline inline and returns when
    # data is ready.
    excluded: set[str] = set()
    if not write_capable:
        excluded.update(AGENT_WRITE_MCP_TOOLS)
    if not interactive:
        excluded.add("run_materialization")
    tools = [t for t in mcp_tools if getattr(t, "name", None) not in excluded]
    from apps.agents.tools.artifact_manager_agent import (  # noqa: PLC0415 — cycle
        create_artifact_manager_tool,
    )
    from apps.agents.tools.canvas_manager_agent import (  # noqa: PLC0415 — cycle
        create_canvas_manager_tool,
    )
    from apps.agents.tools.canvas_tool import create_canvas_read_tool  # noqa: PLC0415 — cycle

    if write_capable:
        tools.append(create_save_learning_tool(workspace, user))
        tools.append(
            create_artifact_manager_tool(
                workspace,
                user,
                mcp_tools or [],
                conversation_id=conversation_id,
            )
        )
    else:
        tools.extend(
            item
            for item in create_artifact_graph_tools(workspace, user, conversation_id)
            if item.name in ARTIFACT_READ_TOOL_NAMES
        )
    if interactive and conversation_id:
        # The canvas is thread-bound; headless (recipe) runs have no thread.
        # The parent keeps a read-only canvas_read for cheap draft questions;
        # all canvas writes are delegated to the Canvas Manager subagent,
        # which read-only workspace members do not get at all.
        tools.append(create_canvas_read_tool(workspace, user, conversation_id))
        # build_agent_graph already folds write_capable into canvas_write; checking
        # both keeps a direct caller that forgets write_capable from failing open.
        if canvas_write and write_capable:
            tools.append(
                create_canvas_manager_tool(
                    workspace,
                    user,
                    mcp_tools or [],
                    conversation_id=conversation_id,
                )
            )
    if write_capable:
        tools.append(create_recipe_tool(workspace, user))
    if not interactive and write_capable:
        tools.append(create_materialization_tool(workspace, user, job_id))
    return tools


__all__ = [
    "ESCALATION_MESSAGE",
    "ESCALATION_METADATA_KEY",
    "ESCALATION_TRIGGER_COUNT",
    "FIXED_MESSAGE_NODES",
    "HEADLESS_ESCALATION_MESSAGE",
    "READ_ONLY_ESCALATION_MESSAGE",
    "READ_ONLY_SEMANTIC_ESCALATION_MESSAGE",
    "SEMANTIC_ESCALATION_MESSAGE",
    "_should_escalate",
    "build_agent_graph",
]
