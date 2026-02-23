"""
LangGraph agent graph builder for the Scout data agent platform.

This module provides the `build_agent_graph` function which assembles the
complete agent graph with self-correction capabilities. The graph structure
implements a retry loop that allows the agent to diagnose and fix errors
automatically, up to a configurable maximum number of retries.

Graph Architecture:
    START -> agent -> should_continue? -> tools -> check_result -> result_ok?
                   |                                                    |
                   +-> END                                    yes -> agent
                                                               |
                                                              no -> diagnose_and_retry -> agent
                                                                    (max 3 retries)

The graph uses:
- ChatAnthropic as the LLM backend
- ToolNode for tool execution
- Custom nodes for error checking and correction
- Optional checkpointer for conversation persistence
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.nodes import check_result_node, diagnose_and_retry_node
from apps.agents.graph.state import AgentState
from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.knowledge.services.retriever import KnowledgeRetriever

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from apps.projects.models import TenantWorkspace
    from apps.users.models import TenantMembership, User

logger = logging.getLogger(__name__)

# MCP tools that require a context ID (tenant_id) injected from state
MCP_TOOL_NAMES = frozenset(
    {
        "list_tables",
        "describe_table",
        "query",
        "get_metadata",
        "run_materialization",
        "get_schema_status",
        "teardown_schema",
    }
)


# Configuration constants
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0


def _llm_tool_schemas(tools: list, hidden_params: list[str]) -> list:
    """Build tool definitions for the LLM with parameters hidden from the schema.

    MCP tools require context IDs (tenant_id, tenant_membership_id, etc.) but
    the LLM shouldn't provide them — they're injected from state.  We give the
    LLM schemas that omit those parameters so it can't hallucinate wrong values.

    Non-MCP tools are returned unchanged.
    """
    hidden = set(hidden_params)
    result: list = []
    for tool in tools:
        if tool.name not in MCP_TOOL_NAMES:
            result.append(tool)
            continue

        schema = tool.get_input_schema().model_json_schema()
        props = schema.get("properties", {})
        to_hide = hidden & set(props)
        if not to_hide:
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
    """Create a graph node that injects state values into MCP tool call args.

    Before the ToolNode executes, this node copies the last AI message and
    injects values from the agent state into every MCP tool call's args.
    ``injections`` maps tool-arg-name → state-field-name.  This ensures the
    MCP server always receives the correct context IDs regardless of what the
    LLM generated.
    """

    async def injecting_node(state: AgentState) -> dict[str, Any]:
        messages = list(state["messages"])
        last_msg = messages[-1]

        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            modified_msg = copy.copy(last_msg)
            modified_calls = []
            for tc in last_msg.tool_calls:
                if tc["name"] in MCP_TOOL_NAMES:
                    extra = {k: state.get(v, "") for k, v in injections.items()}
                    tc = {**tc, "args": {**tc["args"], **extra}}
                modified_calls.append(tc)
            modified_msg.tool_calls = modified_calls
            messages = messages[:-1] + [modified_msg]

        return await base_tool_node.ainvoke({"messages": messages})

    return injecting_node


def build_agent_graph(
    tenant_membership: TenantMembership,
    user: User | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_tools: list | None = None,
    oauth_tokens: dict | None = None,
):
    """
    Build a LangGraph agent graph for a tenant workspace.

    Args:
        tenant_membership: The TenantMembership for the current user.
        user: Optional User model instance.
        checkpointer: Optional LangGraph checkpointer for conversation persistence.
        mcp_tools: List of MCP tools to include.
        oauth_tokens: Optional OAuth tokens for tool authentication.
    """
    from apps.projects.models import TenantWorkspace

    workspace, _ = TenantWorkspace.objects.get_or_create(
        tenant_id=tenant_membership.tenant_id,
        defaults={"tenant_name": tenant_membership.tenant_name},
    )

    logger.info("Building agent graph for tenant:%s", tenant_membership.tenant_id)

    # --- Build tools ---
    tools = _build_tools(workspace, user, mcp_tools or [])
    logger.debug("Created %d tools for tenant:%s", len(tools), tenant_membership.tenant_id)

    # --- Inject tenant_id and tenant_membership_id into MCP tool calls ---
    injections = {
        "tenant_id": "tenant_id",
        "tenant_membership_id": "tenant_membership_id",
    }
    hidden_params = list(injections.keys())

    # --- Build LLM with tools ---
    llm = ChatAnthropic(
        model="claude-sonnet-4-5-20250929",
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )
    llm_tool_schemas = _llm_tool_schemas(tools, hidden_params=hidden_params)
    llm_with_tools = llm.bind_tools(llm_tool_schemas)

    # --- Build system prompt ---
    system_prompt = _build_system_prompt(workspace, tenant_membership)
    logger.debug(
        "System prompt assembled: %d characters for tenant:%s",
        len(system_prompt),
        tenant_membership.tenant_id,
    )

    # --- Create tool node with context ID injection ---
    base_tool_node = ToolNode(tools)
    tool_node = _make_injecting_tool_node(base_tool_node, injections)

    # --- Define graph nodes ---

    def agent_node(state: AgentState) -> dict[str, Any]:
        """
        Call the LLM with the current conversation and system prompt.

        This node prepends the system prompt to the messages and invokes
        the LLM. The LLM may respond with text, tool calls, or both.
        """
        state_messages = list(state["messages"])
        # Filter out any prior system messages to avoid duplicates across cycles
        state_messages = [m for m in state_messages if not isinstance(m, SystemMessage)]
        messages = [SystemMessage(content=system_prompt)] + state_messages
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        """
        Determine if the agent should call tools or end the conversation.

        Checks the last message for tool calls. If present, route to tools.
        Otherwise, end the conversation.
        """
        messages = state.get("messages", [])
        if not messages:
            return END

        last_message = messages[-1]

        # Check if the LLM wants to call tools
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        return END

    def result_ok(state: AgentState) -> Literal["agent", "diagnose"]:
        """
        After checking results, decide whether to proceed or diagnose errors.

        Routes to the diagnosis node if needs_correction is set,
        otherwise continues to the agent for the next response.
        """
        if state.get("needs_correction", False):
            return "diagnose"
        return "agent"

    # --- Build the graph ---
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("check_result", check_result_node)
    graph.add_node("diagnose_and_retry", diagnose_and_retry_node)

    # Set entry point
    graph.set_entry_point("agent")

    # Add edges
    # agent -> should_continue? -> tools or END
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            END: END,
        },
    )

    # tools -> check_result
    graph.add_edge("tools", "check_result")

    # check_result -> result_ok? -> agent or diagnose
    graph.add_conditional_edges(
        "check_result",
        result_ok,
        {
            "agent": "agent",
            "diagnose": "diagnose_and_retry",
        },
    )

    # diagnose_and_retry -> agent (to try the corrected query)
    graph.add_edge("diagnose_and_retry", "agent")

    # --- Compile and return ---
    compiled = graph.compile(checkpointer=checkpointer)

    logger.info(
        "Agent graph compiled for tenant:%s (checkpointer: %s)",
        tenant_membership.tenant_id,
        type(checkpointer).__name__ if checkpointer else "None",
    )

    return compiled


def _build_tools(workspace: TenantWorkspace, user: User | None, mcp_tools: list) -> list:
    """
    Build the tool list for the agent.

    MCP tools (from the Scout MCP server):
    - query: Execute read-only SQL queries
    - list_tables: List available tables
    - describe_table: Get table column details
    - get_metadata: Full schema snapshot

    Local tools (always included):
    - save_learning: For persisting discovered corrections
    - create_artifact: For creating interactive visualizations
    - update_artifact: For updating existing artifacts
    - save_as_recipe: For creating replayable analysis workflows

    Args:
        workspace: The TenantWorkspace model instance.
        user: Optional User for tracking learning discovery.
        mcp_tools: LangChain tools loaded from the MCP server.

    Returns:
        List of LangChain tool functions.
    """
    tools = list(mcp_tools)
    tools.append(create_save_learning_tool(workspace, user))
    tools.extend(create_artifact_tools(workspace, user))
    tools.append(create_recipe_tool(workspace, user))
    return tools


def _build_system_prompt(workspace: TenantWorkspace, tenant_membership: TenantMembership) -> str:
    """
    Assemble the complete system prompt for a tenant workspace.

    The prompt is built from:
    1. BASE_SYSTEM_PROMPT: Core agent behavior and formatting
    2. ARTIFACT_PROMPT_ADDITION: Instructions for creating artifacts
    3. Workspace system prompt: Tenant-specific instructions
    4. Knowledge retriever output: Metrics, rules, learnings
    5. Tenant context: Tenant name, provider, query config

    Args:
        workspace: The TenantWorkspace model instance.
        tenant_membership: The TenantMembership for context metadata.

    Returns:
        Complete system prompt string.
    """
    sections = [BASE_SYSTEM_PROMPT]
    sections.append(ARTIFACT_PROMPT_ADDITION)

    if workspace.system_prompt:
        sections.append(f"\n## Tenant-Specific Instructions\n\n{workspace.system_prompt}\n")

    retriever = KnowledgeRetriever(workspace)
    knowledge_context = retriever.retrieve()
    if knowledge_context:
        sections.append(f"\n## Knowledge Base\n\n{knowledge_context}\n")

    sections.append(f"""
## Tenant Context

- Tenant: {tenant_membership.tenant_name} ({tenant_membership.tenant_id})
- Provider: {tenant_membership.provider}

## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.

You can materialize data from CommCare using the `run_materialization` tool.
""")

    return "\n".join(sections)


__all__ = [
    "build_agent_graph",
]
