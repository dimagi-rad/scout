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
from apps.projects.services.data_dictionary import DataDictionaryGenerator

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from apps.projects.models import Project
    from apps.users.models import TenantMembership, User

logger = logging.getLogger(__name__)


# Configuration constants
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0


def build_agent_graph(
    project: "Project | None" = None,
    user: "User | None" = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
    mcp_tools: list | None = None,
    oauth_tokens: dict | None = None,
    tenant_membership: "TenantMembership | None" = None,
):
    """
    Build a LangGraph agent graph for a project or tenant.

    Accepts either a Project (legacy) or TenantMembership (new tenant flow).
    When tenant_membership is provided, uses MCP tools only (no project-local tools).
    """
    context_label = (
        f"tenant:{tenant_membership.tenant_id}"
        if tenant_membership
        else f"project:{project.slug}" if project else "unknown"
    )
    logger.info("Building agent graph for %s", context_label)

    # --- Build tools ---
    if project:
        tools = _build_tools(project, user, mcp_tools or [])
    else:
        tools = list(mcp_tools or [])
    logger.debug("Created %d tools for %s", len(tools), context_label)

    # --- Build LLM with tools ---
    llm_model = project.llm_model if project else "claude-sonnet-4-5-20250929"
    llm = ChatAnthropic(
        model=llm_model,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )
    llm_with_tools = llm.bind_tools(tools)

    # --- Build system prompt ---
    if tenant_membership and not project:
        system_prompt = _build_tenant_system_prompt(tenant_membership)
    else:
        system_prompt = _build_system_prompt(project)
    logger.debug("System prompt assembled: %d characters for %s", len(system_prompt), context_label)

    # --- Create tool node ---
    tool_node = ToolNode(tools)

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
        "Agent graph compiled for %s (checkpointer: %s)",
        context_label,
        type(checkpointer).__name__ if checkpointer else "None",
    )

    return compiled


def _build_tools(project: "Project", user: "User | None", mcp_tools: list) -> list:
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
    - create_recipe: For creating replayable analysis workflows

    Args:
        project: The Project model instance.
        user: Optional User for tracking learning discovery.
        mcp_tools: LangChain tools loaded from the MCP server.

    Returns:
        List of LangChain tool functions.
    """
    # Start with MCP tools (data access)
    tools = list(mcp_tools)

    # Learning tool (always included)
    learning_tool = create_save_learning_tool(project, user)
    tools.append(learning_tool)

    # Artifact tools (always included)
    artifact_tools = create_artifact_tools(project, user)
    tools.extend(artifact_tools)

    # Recipe tool (always included)
    recipe_tool = create_recipe_tool(project, user)
    tools.append(recipe_tool)

    return tools


def _build_system_prompt(project: "Project") -> str:
    """
    Assemble the complete system prompt from multiple sources.

    The prompt is built from:
    1. BASE_SYSTEM_PROMPT: Core agent behavior and formatting
    2. Project system prompt: Project-specific instructions
    3. Knowledge retriever output: Metrics, rules, learnings
    4. Data dictionary: Schema information

    For large schemas, the data dictionary is abbreviated and the agent
    should use the describe_table tool for details.

    Args:
        project: The Project model instance.

    Returns:
        Complete system prompt string.
    """
    sections = [BASE_SYSTEM_PROMPT]

    # Artifact creation instructions
    sections.append(ARTIFACT_PROMPT_ADDITION)

    # Project-specific system prompt
    if project.system_prompt:
        sections.append(f"""
## Project-Specific Instructions

{project.system_prompt}
""")

    # Knowledge retriever output (metrics, rules, learnings)
    retriever = KnowledgeRetriever(project)
    knowledge_context = retriever.retrieve()

    if knowledge_context:
        sections.append(f"""
## Project Knowledge Base

{knowledge_context}
""")

    # Data dictionary
    dd_generator = DataDictionaryGenerator(project)
    dd_text = dd_generator.render_for_prompt()

    sections.append(f"""
## Database Schema

{dd_text}
""")

    # Query configuration
    sections.append(f"""
## Query Configuration

- Project ID: {project.id}
- Maximum rows per query: {project.max_rows_per_query}
- Query timeout: {project.max_query_timeout_seconds} seconds
- Schema: {project.db_schema}

**Important:** When using the `query`, `list_tables`, `describe_table`, or `get_metadata` tools, always pass `project_id` = "{project.id}".

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")

    return "\n".join(sections)


def _build_tenant_system_prompt(tenant_membership: "TenantMembership") -> str:
    """Build a system prompt for tenant-based (non-project) conversations."""
    sections = [BASE_SYSTEM_PROMPT]

    sections.append(f"""
## Tenant Context

- Tenant: {tenant_membership.tenant_name} ({tenant_membership.tenant_id})
- Provider: {tenant_membership.provider}

## Query Configuration

- Tenant ID: {tenant_membership.tenant_id}
- Maximum rows per query: 500
- Query timeout: 30 seconds
- Schema: {tenant_membership.tenant_id}

**Important:** When using the `query`, `list_tables`, `describe_table`, or `get_metadata` tools, \
always pass `tenant_id` = "{tenant_membership.tenant_id}".

When results are truncated, suggest adding filters or using aggregations to reduce the result size.

You can materialize data from CommCare using the `run_materialization` tool with \
`tenant_id` = "{tenant_membership.tenant_id}".
""")

    return "\n".join(sections)


__all__ = [
    "build_agent_graph",
]
