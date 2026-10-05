"""Tool binding for the main agent graph.

Which tools a run gets for its role and mode, the model-facing schemas with the
server-injected context params hidden, and the tool node that injects those
params from agent state before each call.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode

from apps.agents.graph.run_policy import (
    _artifact_manager_task_is_missing,
    _synthesize_artifact_manager_task,
)
from apps.agents.graph.state import AgentState
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

if TYPE_CHECKING:
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
