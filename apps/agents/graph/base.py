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
from apps.agents.tools.describe_table import create_describe_table_tool
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.agents.tools.sql_tool import create_sql_tool
from apps.knowledge.services.retriever import KnowledgeRetriever
from apps.projects.services.data_dictionary import DataDictionaryGenerator

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from apps.projects.models import Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


# Configuration constants
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0
LARGE_SCHEMA_THRESHOLD = 15  # Number of tables above which describe_table is added


def build_agent_graph(
    project: "Project",
    user: "User | None" = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
):
    """
    Build a LangGraph agent graph for a specific project.

    This function assembles all components of the Scout agent:
    1. Creates project-scoped tools (SQL execution, table description, learning)
    2. Binds tools to the LLM (ChatAnthropic)
    3. Assembles the system prompt from multiple sources
    4. Builds the graph with self-correction loop
    5. Compiles with optional checkpointer for persistence

    The resulting graph handles:
    - User questions about data
    - SQL query generation and execution
    - Automatic error detection and correction (up to 3 retries)
    - Learning from successful corrections
    - Knowledge-grounded responses using canonical metrics and business rules

    Args:
        project: The Project model instance containing database connection
            settings, system prompt, and configuration.
        user: Optional User model instance for tracking who triggered learnings.
            If None, learnings will have no associated user.
        checkpointer: Optional LangGraph checkpointer for conversation persistence.
            If None, conversations won't persist between sessions.
            Use MemorySaver for development, PostgresSaver for production.

    Returns:
        A compiled LangGraph that can be invoked with:
            graph.invoke(
                {
                    "messages": [HumanMessage(content="...")],
                    "project_id": str(project.id),
                    "project_name": project.name,
                    "user_id": str(user.id) if user else "",
                    "user_role": "analyst",
                    "needs_correction": False,
                    "retry_count": 0,
                    "correction_context": {},
                },
                config={"configurable": {"thread_id": "unique-thread-id"}}
            )

    Example:
        >>> from apps.projects.models import Project
        >>> from langgraph.checkpoint.memory import MemorySaver
        >>>
        >>> project = Project.objects.get(slug="analytics")
        >>> checkpointer = MemorySaver()
        >>> graph = build_agent_graph(project, checkpointer=checkpointer)
        >>>
        >>> result = graph.invoke({
        ...     "messages": [HumanMessage(content="How many users signed up last month?")],
        ...     "project_id": str(project.id),
        ...     "project_name": project.name,
        ...     "user_id": "",
        ...     "user_role": "analyst",
        ...     "needs_correction": False,
        ...     "retry_count": 0,
        ...     "correction_context": {},
        ... }, config={"configurable": {"thread_id": "thread-123"}})
    """
    logger.info("Building agent graph for project: %s", project.slug)

    # --- Build tools ---
    tools = _build_tools(project, user)
    logger.debug("Created %d tools for project %s", len(tools), project.slug)

    # --- Build LLM with tools ---
    llm = ChatAnthropic(
        model=project.llm_model,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )
    llm_with_tools = llm.bind_tools(tools)

    # --- Build system prompt ---
    system_prompt = _build_system_prompt(project)
    logger.debug(
        "System prompt assembled: %d characters for project %s",
        len(system_prompt),
        project.slug,
    )

    # --- Create tool node ---
    tool_node = ToolNode(tools)

    # --- Define graph nodes ---

    def agent_node(state: AgentState) -> dict[str, Any]:
        """
        Call the LLM with the current conversation and system prompt.

        This node prepends the system prompt to the messages and invokes
        the LLM. The LLM may respond with text, tool calls, or both.
        """
        messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
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
        "Agent graph compiled for project %s (checkpointer: %s)",
        project.slug,
        type(checkpointer).__name__ if checkpointer else "None",
    )

    return compiled


def _build_tools(project: "Project", user: "User | None") -> list:
    """
    Build the tool list for the agent.

    Always includes:
    - execute_sql: For running queries against the project database
    - save_learning: For persisting discovered corrections
    - create_artifact: For creating interactive visualizations
    - update_artifact: For updating existing artifacts

    Conditionally includes:
    - describe_table: For large schemas (>15 tables) where full details
      can't fit in the system prompt

    Args:
        project: The Project model instance.
        user: Optional User for tracking learning discovery.

    Returns:
        List of LangChain tool functions.
    """
    tools = []

    # SQL execution tool (always included)
    sql_tool = create_sql_tool(project)
    tools.append(sql_tool)

    # Learning tool (always included)
    # Create a placeholder user if none provided
    learning_tool = create_save_learning_tool(project, user)
    tools.append(learning_tool)

    # Artifact tools (always included)
    artifact_tools = create_artifact_tools(project, user)
    tools.extend(artifact_tools)

    # Recipe tool (always included)
    recipe_tool = create_recipe_tool(project, user)
    tools.append(recipe_tool)

    # Describe table tool (for large schemas)
    dd = project.data_dictionary or {}
    table_count = len(dd.get("tables", {}))

    if table_count > LARGE_SCHEMA_THRESHOLD:
        logger.debug(
            "Adding describe_table tool (schema has %d tables, threshold is %d)",
            table_count,
            LARGE_SCHEMA_THRESHOLD,
        )
        describe_tool = create_describe_table_tool(project)
        tools.append(describe_tool)

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

- Maximum rows per query: {project.max_rows_per_query}
- Query timeout: {project.max_query_timeout_seconds} seconds
- Schema: {project.db_schema}

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")

    return "\n".join(sections)


__all__ = [
    "build_agent_graph",
]
