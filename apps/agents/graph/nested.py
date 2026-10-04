"""Shared LangGraph builder for the nested manager subagents (canvas, artifact).

Callers build their own tools, so caller-specific arguments (e.g. the canvas
``human_turn``) never reach this module.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Literal

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.state import (
    TRUNCATED_TOOL_CALLS_NODE,
    AgentState,
    reject_truncated_tool_calls,
    truncated_retries_exhausted,
    truncated_tool_calls,
)
from apps.agents.llm_request import SUBAGENT_EFFORT, chat_model_kwargs
from apps.agents.subagents.events import SUBAGENT_EVENT_QUEUE_CONFIG_KEY
from apps.agents.tool_results import compact_tool_results

NESTED_MCP_TOOL_NAMES = frozenset({"list_datasets", "describe_dataset", "semantic_query"})
# Sized for adaptive thinking plus the reply; see DEFAULT_MAX_TOKENS.
NESTED_MAX_TOKENS = 16_000


def build_nested_graph(
    *,
    local_tools: list,
    mcp_tools: list,
    prompt_supplier: Callable[[], str],
):
    """Compile the agent/tools loop; ``prompt_supplier`` runs on every model turn."""
    nested_mcp_tools = [
        tool_obj
        for tool_obj in mcp_tools
        if getattr(tool_obj, "name", None) in NESTED_MCP_TOOL_NAMES
    ]
    tools = [*local_tools, *nested_mcp_tools]
    tool_node = _make_nested_tool_node(ToolNode(tools))
    llm = ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=NESTED_MAX_TOKENS,
        **chat_model_kwargs(SUBAGENT_EFFORT),
    )
    llm_with_tools = llm.bind_tools(_nested_llm_tool_schemas(tools))

    async def agent_node(state: AgentState) -> dict[str, Any]:
        state_messages = [m for m in list(state["messages"]) if not isinstance(m, SystemMessage)]
        messages = [SystemMessage(content=prompt_supplier()), *state_messages]
        response = await llm_with_tools.ainvoke(messages)
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "truncated_tool_calls", "__end__"]:
        messages = state.get("messages", [])
        if not messages:
            return END
        last_message = messages[-1]
        if truncated_tool_calls(last_message):
            return TRUNCATED_TOOL_CALLS_NODE
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        return END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node(TRUNCATED_TOOL_CALLS_NODE, reject_truncated_tool_calls)
    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", TRUNCATED_TOOL_CALLS_NODE: TRUNCATED_TOOL_CALLS_NODE, END: END},
    )
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges(
        TRUNCATED_TOOL_CALLS_NODE,
        lambda state: END if truncated_retries_exhausted(state["messages"]) else "agent",
        {"agent": "agent", END: END},
    )
    return graph.compile()


def _nested_llm_tool_schemas(tools: list) -> list:
    hidden = {
        "workspace_id",
        "user_id",
        "thread_id",
        "tool_call_id",
        "runtime",
        SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    }
    result = []
    for tool_obj in tools:
        schema = tool_obj.get_input_schema().model_json_schema()
        props = schema.get("properties", {})
        to_hide = hidden & set(props)
        if not to_hide:
            result.append(tool_obj)
            continue
        trimmed_props = {k: v for k, v in props.items() if k not in to_hide}
        trimmed_required = [r for r in schema.get("required", []) if r not in to_hide]
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool_obj.name,
                    "description": tool_obj.description or "",
                    "parameters": {
                        "type": "object",
                        "properties": trimmed_props,
                        "required": trimmed_required,
                    },
                },
            }
        )
    return result


def _make_nested_tool_node(base_tool_node: ToolNode):
    async def injecting_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        messages = list(state["messages"])
        last_msg = messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            modified_msg = copy.copy(last_msg)
            modified_calls = []
            for tc in last_msg.tool_calls:
                if tc["name"] in NESTED_MCP_TOOL_NAMES:
                    tc_id = tc.get("id") or ""
                    extra = {
                        "workspace_id": state.get("workspace_id", ""),
                        "user_id": state.get("user_id", ""),
                        "thread_id": state.get("thread_id", ""),
                        "tool_call_id": tc_id,
                    }
                    tc = {**tc, "args": {**tc["args"], **extra}}
                modified_calls.append(tc)
            modified_msg.tool_calls = modified_calls
            messages = [*messages[:-1], modified_msg]
        return compact_tool_results(
            await base_tool_node.ainvoke({"messages": messages}, config=config),
            NESTED_MCP_TOOL_NAMES,
        )

    injecting_node.__annotations__["config"] = RunnableConfig | None
    return injecting_node
