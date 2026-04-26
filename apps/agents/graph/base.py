"""
LangGraph agent graph builder for the Scout data agent platform.

This module provides the `build_agent_graph` function which assembles the
agent graph. The graph uses a simple loop: agent -> tools -> agent, relying
on the LLM to self-correct from error ToolMessages naturally. A recursion
limit prevents runaway loops.

Graph Architecture:
    START -> agent -> should_continue? -> tools -> agent
                   |
                   +-> END

The graph uses:
- ChatAnthropic as the LLM backend
- ToolNode for tool execution
- Optional checkpointer for conversation persistence
"""

from __future__ import annotations

import copy
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.state import AgentState
from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.knowledge.services.retriever import KnowledgeRetriever
from apps.workspaces.models import SchemaState, TenantSchema
from mcp_server.context import load_tenant_context
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_list_tables,
    transformation_aware_list_tables,
)

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
        "run_materialization",
        "get_schema_status",
        "teardown_schema",
        "get_lineage",
    }
)


# Configuration constants
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0
SCHEMA_CONTEXT_CHAR_BUDGET = 6000

# Simple TTL cache for system prompts
_system_prompt_cache: dict[str, tuple[str, float]] = {}
_SYSTEM_PROMPT_TTL = 60  # 60 seconds — short to limit staleness from knowledge/schema changes


def _system_prompt_cache_key(workspace, user) -> str:
    """Build a cache key from workspace + user properties that affect the prompt.

    Includes user.id because _fetch_schema_context scopes TenantMetadata
    lookup to the specific user. Includes workspace.system_prompt hash
    so edits invalidate immediately.
    """
    prompt_hash = hashlib.md5(
        (workspace.system_prompt or "").encode(), usedforsecurity=False
    ).hexdigest()[:8]
    user_id = getattr(user, "id", "anon")
    return f"{workspace.id}:{user_id}:{prompt_hash}"


def _render_compact_schema(tables: list[dict], last_materialized_at: str | None) -> str:
    """Render a compact schema block: table names, descriptions, row counts."""
    lines = []
    if last_materialized_at:
        lines.append(f"Data is loaded and ready. Last updated: {last_materialized_at}\n")
    else:
        lines.append("Data is loaded and ready.\n")

    lines.append("### Available Tables\n")
    lines.append("| Table | Description | Rows |")
    lines.append("|---|---|---|")
    for t in tables:
        row_count = f"{t['row_count']:,}" if t.get("row_count") is not None else "unknown"
        desc = t.get("description") or ""
        lines.append(f"| {t['name']} | {desc} | {row_count} |")

    lines.append("\nUse the `describe_table` tool for column details.")
    return "\n".join(lines)


def _render_full_schema(
    tables: list[dict],
    column_map: dict[str, list[dict]],
    last_materialized_at: str | None,
) -> str:
    """Render a full schema block with column details per table."""
    lines = []
    if last_materialized_at:
        lines.append(f"Data is loaded and ready. Last updated: {last_materialized_at}\n")
    else:
        lines.append("Data is loaded and ready.\n")

    lines.append("### Available Tables\n")
    for t in tables:
        row_count = f"{t['row_count']:,}" if t.get("row_count") is not None else "unknown"
        desc = t.get("description") or ""
        header = f"**{t['name']}**"
        if desc:
            header += f" — {desc}"
        header += f" ({row_count} rows)"
        lines.append(header)

        cols = column_map.get(t["name"], [])
        if cols:
            lines.append("Columns:")
            for col in cols:
                col_desc = f" — {col['description']}" if col.get("description") else ""
                lines.append(f"- {col['name']} ({col['type']}){col_desc}")
        lines.append("")

    return "\n".join(lines)


async def _fetch_schema_context(tenant, user) -> str:
    """Fetch database schema state and build a ## Data Availability prompt section.

    Tries to build a full schema block (tables + columns). Falls back to a compact
    block (tables + row counts only) if the full text exceeds SCHEMA_CONTEXT_CHAR_BUDGET.
    """
    ts = await TenantSchema.objects.filter(
        tenant=tenant,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    registry = get_registry()
    pipeline_config = registry.get_by_provider(tenant.provider)
    pipeline_name = pipeline_config.name if pipeline_config else "commcare_sync"

    if ts is None:
        return (
            "No data has been loaded yet. "
            f'Call `run_materialization` to start loading data (pipeline: "{pipeline_name}"). '
            "run_materialization returns immediately with a run_id — then poll "
            "`get_materialization_status` with that run_id until the status is 'completed'."
        )

    if ts.state == SchemaState.MATERIALIZING:
        return (
            "Data is currently loading — this usually takes a minute. "
            "Ask the user to retry shortly. Do NOT trigger another data load."
        )

    # Schema is active: fetch table list
    if pipeline_config is None:
        pipeline_config = registry.get("commcare_sync")

    # Try transformation-aware listing (prefers terminal models over replaced ones)
    from apps.transformations.services.lineage import aget_terminal_assets

    terminal_assets = await aget_terminal_assets(tenant_ids=[tenant.id])

    if terminal_assets:
        tables = await transformation_aware_list_tables(ts, pipeline_config, tenant_ids=[tenant.id])
    else:
        tables = await pipeline_list_tables(ts, pipeline_config)

    if not tables:
        return "Data is loaded but no tables are available yet. The materialization may still be completing."

    last_materialized_at = tables[0].get("materialized_at") if tables else None

    # Try full schema with columns
    try:
        ctx = await load_tenant_context(tenant.external_id)
        from apps.workspaces.models import TenantMetadata

        tenant_metadata = await TenantMetadata.objects.filter(
            tenant_membership__tenant=tenant, tenant_membership__user=user
        ).afirst()

        column_map: dict[str, list[dict]] = {}
        for t in tables:
            detail = await pipeline_describe_table(t["name"], ctx, tenant_metadata, pipeline_config)
            if detail:
                column_map[t["name"]] = detail.get("columns", [])

        full_text = _render_full_schema(tables, column_map, last_materialized_at)

        # Add lineage tool hint when transformation assets exist
        if terminal_assets:
            full_text += (
                "\n\nThese tables are produced by a transformation pipeline. "
                "Use the `get_lineage` tool to explore how any table was built."
            )

        if len(full_text) <= SCHEMA_CONTEXT_CHAR_BUDGET:
            return full_text
    except Exception:
        logger.debug(
            "Could not fetch full schema for context injection, using compact", exc_info=True
        )

    # Fall back to compact
    compact = _render_compact_schema(tables, last_materialized_at)
    if terminal_assets:
        compact += (
            "\n\nThese tables are produced by a transformation pipeline. "
            "Use the `get_lineage` tool to explore how any table was built."
        )
    return compact


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
            messages = [*messages[:-1], modified_msg]

        return await base_tool_node.ainvoke({"messages": messages})

    return injecting_node


async def build_agent_graph(
    workspace: Workspace,
    user: User | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_tools: list | None = None,
    oauth_tokens: dict | None = None,
):
    """
    Build a LangGraph agent graph for a workspace.

    Args:
        workspace: The Workspace model instance.
        user: Optional User model instance.
        checkpointer: Optional LangGraph checkpointer for conversation persistence.
        mcp_tools: List of MCP tools to include.
        oauth_tokens: Optional OAuth tokens for tool authentication.
    """
    logger.info("Building agent graph for workspace %s", workspace.id)

    # --- Build tools ---
    tools = _build_tools(workspace, user, mcp_tools or [])
    logger.debug("Created %d tools for workspace %s", len(tools), workspace.id)

    # --- Inject workspace_id and user_id into MCP tool calls from agent state ---
    injections = {"workspace_id": "workspace_id", "user_id": "user_id"}
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
    system_prompt = await _build_system_prompt(workspace, user)
    logger.debug(
        "System prompt assembled: %d characters for workspace %s",
        len(system_prompt),
        workspace.id,
    )

    # --- Create tool node with context ID injection ---
    base_tool_node = ToolNode(tools)
    tool_node = _make_injecting_tool_node(base_tool_node, injections)

    # --- Define graph nodes ---

    async def agent_node(state: AgentState) -> dict[str, Any]:
        """
        Call the LLM with the current conversation and system prompt.

        This node prepends the system prompt to the messages and invokes
        the LLM. The LLM may respond with text, tool calls, or both.
        """
        state_messages = list(state["messages"])
        # Filter out any prior system messages to avoid duplicates across cycles
        state_messages = [m for m in state_messages if not isinstance(m, SystemMessage)]

        # Defensive guard: ensure every AIMessage with tool_calls is followed
        # by matching ToolMessages. If not, inject synthetic error ToolMessages
        # so Anthropic never receives an invalid tool_use/tool_result sequence.
        answered_ids: set[str] = {
            m.tool_call_id for m in state_messages if isinstance(m, ToolMessage) and m.tool_call_id
        }
        repaired: list = []
        for msg in state_messages:
            repaired.append(msg)
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in answered_ids:
                        logger.warning(
                            "agent_node: found dangling tool_call_id=%s tool_name=%s — "
                            "injecting synthetic tool_result to satisfy Anthropic protocol",
                            tc_id,
                            tc.get("name", "unknown"),
                        )
                        repaired.append(
                            ToolMessage(
                                content=(
                                    "Tool call was interrupted — the user sent a new message "
                                    "before this tool completed."
                                ),
                                tool_call_id=tc_id,
                                name=tc.get("name", "unknown"),
                            )
                        )
                        answered_ids.add(tc_id)

        messages = [SystemMessage(content=system_prompt), *repaired]
        response = await llm_with_tools.ainvoke(messages)
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

    # --- Build the graph ---
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)

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

    # tools -> agent (the LLM sees error ToolMessages and self-corrects)
    graph.add_edge("tools", "agent")

    # --- Compile and return ---
    compiled = graph.compile(checkpointer=checkpointer)

    logger.info(
        "Agent graph compiled for workspace %s (checkpointer: %s)",
        workspace.id,
        type(checkpointer).__name__ if checkpointer else "None",
    )

    return compiled


def _build_tools(workspace: Workspace, user: User | None, mcp_tools: list) -> list:
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
        workspace: The Workspace model instance.
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


async def _build_system_prompt(workspace: Workspace, user) -> str:
    """
    Assemble the complete system prompt for a workspace.

    The prompt is built from:
    1. BASE_SYSTEM_PROMPT: Core agent behavior and formatting
    2. ARTIFACT_PROMPT_ADDITION: Instructions for creating artifacts
    3. Workspace system prompt: Workspace-specific instructions
    4. Knowledge retriever output: Metrics, rules, learnings
    5. Tenant context: Tenant name, provider, query config (single-tenant only)

    Args:
        workspace: The Workspace model instance.
        user: The User model instance (used to scope tenant metadata lookup).

    Returns:
        Complete system prompt string.
    """
    cache_key = _system_prompt_cache_key(workspace, user)
    cached = _system_prompt_cache.get(cache_key)
    if cached is not None:
        value, timestamp = cached
        if time.monotonic() - timestamp < _SYSTEM_PROMPT_TTL:
            return value

    sections = [BASE_SYSTEM_PROMPT]
    sections.append(ARTIFACT_PROMPT_ADDITION)

    if workspace.system_prompt:
        sections.append(f"\n## Workspace Instructions\n\n{workspace.system_prompt}\n")

    retriever = KnowledgeRetriever(workspace)
    knowledge_context = await retriever.retrieve()
    if knowledge_context:
        sections.append(f"\n## Knowledge Base\n\n{knowledge_context}\n")

    tenant_count = await workspace.tenants.acount()

    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        pipeline_config = get_registry().get_by_provider(tenant.provider)
        pipeline_name = pipeline_config.name if pipeline_config else "commcare_sync"

        sections.append(f"""
## Tenant Context

- Tenant: {tenant.canonical_name} ({tenant.external_id})
- Provider: {tenant.get_provider_display()}
- Pipeline: {pipeline_name}

## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")

        # Pre-fetch schema state and table metadata — no need to call get_schema_status at runtime.
        schema_context = await _fetch_schema_context(tenant, user)
        sections.append(f"\n## Data Availability\n\n{schema_context}\n")
    elif tenant_count > 1:
        sections.append("""
## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")
        sections.append(
            "\n## Data Availability\n\n"
            "This is a multi-tenant workspace. Tables are prefixed with the tenant name "
            "using double underscore: `{tenant_name}__{table_name}`.\n"
            "To query across tenants, use explicit JOINs between namespaced tables.\n"
            "Call `list_tables` to see all available tables.\n"
        )

    result = "\n".join(sections)

    _system_prompt_cache[cache_key] = (result, time.monotonic())

    # Evict expired entries to prevent unbounded growth
    if len(_system_prompt_cache) > 256:
        now = time.monotonic()
        expired = [
            k for k, (_, ts) in _system_prompt_cache.items() if now - ts > _SYSTEM_PROMPT_TTL
        ]
        for k in expired:
            del _system_prompt_cache[k]

    return result


__all__ = [
    "build_agent_graph",
]
