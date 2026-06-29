"""
LangGraph agent graph builder for the Scout data agent platform.

`build_agent_graph` assembles a loop (agent -> tools -> agent) that relies on
the LLM to self-correct from error ToolMessages; a recursion limit bounds runaway
loops and a panic-loop detector escalates after repeated schema errors.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.state import AgentState, prune_messages
from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.materialization_tool import create_materialization_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.knowledge.services.retriever import KnowledgeRetriever
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceViewSchema,
)
from mcp_server.context import load_tenant_context, load_workspace_context
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_list_tables,
    transformation_aware_list_tables,
    workspace_list_tables,
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
        "get_lineage",
        # workspace_id is injected into these so an LLM-supplied run_id is scoped
        # to the calling workspace (arch #253, 01#6).
        "get_materialization_status",
        "cancel_materialization",
    }
)

# MCP tools the server advertises but that must NEVER be bound to the LLM.
#
# ``teardown_schema`` (arch #237 / finding 00#2) DROPs all tenant/view schemas
# but updates no Django state (TenantSchema/MaterializationRun/WorkspaceViewSchema
# stay stale) and silently destroys sibling workspaces sharing the schema. Its
# only guards are an LLM-suppliable ``confirm`` flag and workspace existence — no
# role check — and the agent has no use for it (schemas re-provision on the next
# materialization). Filtered here, not removed from the server (operator/HTTP
# callers still use it).
AGENT_EXCLUDED_MCP_TOOLS = frozenset({"teardown_schema"})

# Context params the graph injects server-side into every MCP tool call. Hidden
# from the LLM-facing schema AND stripped from tool input surfaced to the UI
# (internal ids, not user args). ``tool_call_id`` is injected per-call from the
# tool_call's own id; the rest from agent state. Single source of truth so the
# SSE stream's input-redaction stays in lockstep with the graph's injection.
INJECTED_TOOL_PARAMS = frozenset({"workspace_id", "user_id", "thread_id", "tool_call_id"})


DEFAULT_MAX_TOKENS = 4096
SCHEMA_CONTEXT_CHAR_BUDGET = 6000

# Anthropic prompt-caching breakpoint (arch #254, finding 02#3).
# Default 5-min ephemeral TTL breaks even at ~2 reads, which a single agent turn
# (K+1 LLM calls sharing one prefix) clears immediately. A 1h TTL
# ({"type": "ephemeral", "ttl": "1h"}) costs 2x to write / ~3 reads to pay off —
# use only for bursty traffic with multi-minute idle gaps. langchain-anthropic
# renders tools -> system -> messages, so a breakpoint on the last system block
# caches tool schemas + frozen system prefix together; a second breakpoint via
# the ``cache_control`` kwarg to ``ainvoke`` caches the replayed history.
PROMPT_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

# Panic-loop circuit breaker: if the last N tool messages all carry one of these
# error codes, route to the escalation node so the turn ends with an explicit ask
# instead of burning the recursion budget. These are bare ``error.code`` values
# from the MCP envelope, matched against the parsed JSON field — NOT a substring
# search: substring-matching ``'"code": "NOT_FOUND"'`` only worked under
# FastMCP's indent=2 and would silently break under compact separators (06#1).
# Single source of truth shared with base_system.py's "When the Schema is Broken".
ESCALATION_ERROR_CODES = frozenset({"NOT_FOUND", "VALIDATION_ERROR"})
ESCALATION_TRIGGER_COUNT = 3


def _tool_message_error_code(content: Any) -> str | None:
    """Extract the MCP envelope ``error.code`` from a ToolMessage's content.

    Content may be a JSON string, a list of content blocks (the
    langchain_mcp_adapters shape), or already-parsed structures. Reads the
    structured ``error.code`` rather than a whitespace-sensitive substring (06#1).
    Returns None when the content isn't a recognizable error envelope.
    """
    if isinstance(content, list):
        for block in content:
            text = None
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
            elif isinstance(block, str):
                text = block
            if text:
                code = _tool_message_error_code(text)
                if code is not None:
                    return code
        return None
    if isinstance(content, dict):
        envelope = content
    elif isinstance(content, str):
        try:
            envelope = json.loads(content)
        except (ValueError, TypeError):
            return None
    else:
        return None
    if not isinstance(envelope, dict) or envelope.get("success") is not False:
        return None
    error = envelope.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) else None
    return None


ESCALATION_MESSAGE = (
    "I've encountered repeated schema errors — the tables I expected to "
    "find aren't queryable. The data may need to be re-materialized. "
    "Would you like me to run materialization?"
)


def _should_escalate(messages: list) -> bool:
    """Detect a panic loop: last N trailing tool messages all returned an
    escalation error code. A successful tool call in between resets the streak.
    Matches the structured ``error.code`` (06#1), not a substring.
    """
    streak: list[ToolMessage] = []
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            streak.append(msg)
            if len(streak) >= ESCALATION_TRIGGER_COUNT:
                break
        elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            continue
        else:
            break

    if len(streak) < ESCALATION_TRIGGER_COUNT:
        return False

    for tm in streak:
        code = _tool_message_error_code(tm.content)
        if code not in ESCALATION_ERROR_CODES:
            return False
    return True


_system_prompt_cache: dict[str, tuple[str, float]] = {}
_SYSTEM_PROMPT_TTL = 60  # short, to limit staleness from knowledge/schema changes


def _system_prompt_cache_key(workspace, user, interactive: bool = True) -> str:
    """Build a cache key from workspace + user properties that affect the prompt.

    user.id: _fetch_schema_context scopes TenantMetadata lookup per-user.
    system_prompt hash: edits invalidate immediately.
    interactive: materialization guidance differs (fire-and-resume vs blocking).
    """
    prompt_hash = hashlib.md5(
        (workspace.system_prompt or "").encode(), usedforsecurity=False
    ).hexdigest()[:8]
    user_id = getattr(user, "id", "anon")
    mode = "i" if interactive else "h"
    return f"{workspace.id}:{user_id}:{prompt_hash}:{mode}"


def _render_compact_schema(tables: list[dict], last_materialized_at: str | None) -> str:
    """Render a compact schema block: table names, descriptions, row counts."""
    lines = []
    if last_materialized_at:
        lines.append(f"Data is loaded and ready. Last updated: {last_materialized_at}\n")
    else:
        lines.append("Data is loaded and ready.\n")

    lines.append("### Available Tables\n")
    lines.append("| Table | Description | Materialized Rows |")
    lines.append("|---|---|---|")
    for t in tables:
        materialized = t.get("materialized_row_count")
        row_count = f"{materialized:,}" if materialized is not None else "unknown"
        desc = t.get("description") or ""
        lines.append(f"| {t['name']} | {desc} | {row_count} |")

    lines.append(
        "\nThe `Materialized Rows` column is the count at the last "
        "materialization — not a live count. Do not quote it as an answer; "
        "run `SELECT COUNT(*)` to get a verified value."
    )
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
        materialized = t.get("materialized_row_count")
        row_count = f"{materialized:,}" if materialized is not None else "unknown"
        desc = t.get("description") or ""
        header = f"**{t['name']}**"
        if desc:
            header += f" — {desc}"
        header += f" ({row_count} rows at last materialization)"
        lines.append(header)

        cols = column_map.get(t["name"], [])
        if cols:
            lines.append("Columns:")
            for col in cols:
                col_desc = f" — {col['description']}" if col.get("description") else ""
                lines.append(f"- {col['name']} ({col['type']}){col_desc}")
        lines.append("")

    return "\n".join(lines)


# HEADLESS (non-interactive, e.g. recipe) guidance. No Thread/checkpointer/resume
# path, so the agent must NOT "end its turn and wait" — the headless
# run_materialization tool BLOCKS and the agent continues in the same run.
# (Deliberately avoids the substring "end your turn".)
_HEADLESS_MATERIALIZE_GUIDANCE = (
    "No data has been loaded yet. Call `run_materialization` to load it. This "
    "tool BLOCKS and returns a status summary once loading finishes — keep "
    "working in the same run rather than stopping. After it returns "
    "`status: completed`, continue with the requested analysis; the data is ready."
)

# Headless guidance when a materialization is ALREADY in progress. The tool waits
# for the in-flight load rather than starting a parallel one, so we route to it —
# but must NOT say "no data loaded" (reads as "start one").
_HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE = (
    "A data load is already in progress for this workspace. Call "
    "`run_materialization` to ensure fresh data — it WAITS for the in-progress "
    "load to finish (it does not start a parallel one) and returns when the data "
    "is ready. Then continue with the requested analysis in the same run."
)


async def _fetch_schema_context(tenant, user, interactive: bool = True) -> str:
    """Fetch database schema state and build a ## Data Availability prompt section.

    Tries to build a full schema block (tables + columns). Falls back to a compact
    block (tables + row counts only) if the full text exceeds SCHEMA_CONTEXT_CHAR_BUDGET.

    ``interactive`` selects the materialization guidance: fire-and-resume (chat)
    vs blocking (headless recipe runs).
    """
    ts = await TenantSchema.objects.filter(
        tenant=tenant,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    registry = get_registry()
    pipeline_config = registry.get_by_provider(tenant.provider)

    if ts is None:
        if not interactive:
            return _HEADLESS_MATERIALIZE_GUIDANCE
        # No `pipeline=` arg: run_materialization's LLM-facing schema is empty
        # (all params injected server-side); naming an argument it can't accept
        # confused the agent (finding 02#6).
        return (
            "No data has been loaded yet. Call `run_materialization` to start "
            "loading. This tool returns IMMEDIATELY with `status: started` — do "
            "NOT call other data tools in the same turn. Acknowledge to the user "
            "in ONE sentence and end your turn. The system will resume the "
            "conversation automatically when materialization completes."
        )

    if ts.state == SchemaState.MATERIALIZING:
        if not interactive:
            return _HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE
        return (
            "A materialization is already in progress in the background. Do NOT "
            "trigger another one and do NOT call other data tools (the data is "
            "not yet ready). Briefly tell the user it's still loading and end "
            "your turn — the system will resume the conversation automatically "
            "when the current materialization completes."
        )

    if pipeline_config is None:
        pipeline_config = registry.get("commcare_sync")

    # transformation-aware listing prefers terminal models over replaced ones
    from apps.transformations.services.lineage import aget_terminal_assets

    terminal_assets = await aget_terminal_assets(tenant_ids=[tenant.id])

    if terminal_assets:
        tables = await transformation_aware_list_tables(ts, pipeline_config, tenant_ids=[tenant.id])
    else:
        tables = await pipeline_list_tables(ts, pipeline_config)

    if not tables:
        return "Data is loaded but no tables are available yet. The materialization may still be completing."

    last_materialized_at = tables[0].get("materialized_at") if tables else None

    try:
        ctx = await load_tenant_context(tenant.external_id, tenant.provider)
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

    compact = _render_compact_schema(tables, last_materialized_at)
    if terminal_assets:
        compact += (
            "\n\nThese tables are produced by a transformation pipeline. "
            "Use the `get_lineage` tool to explore how any table was built."
        )
    return compact


_MULTI_TENANT_NAMESPACE_HINT = (
    "This is a multi-tenant workspace. Tables are namespaced views prefixed with the "
    "tenant name using double underscore: `{tenant_name}__{table_name}`. "
    "To query across tenants, use explicit JOINs between namespaced tables."
)


async def _fetch_multi_tenant_schema_context(workspace, user, interactive: bool = True) -> str:
    """Build the ## Data Availability block for a multi-tenant workspace.

    Mirrors `_fetch_schema_context` but consults `WorkspaceViewSchema` plus the
    per-tenant `MaterializationRun` records, so the agent knows up front whether
    data is loaded, still materializing, or missing — without having to call
    `list_tables` first to discover the state.
    """
    vs = await WorkspaceViewSchema.objects.filter(workspace_id=workspace.id).afirst()

    tenant_ids = [t.id async for t in workspace.tenants.all()]

    active_run = None
    if tenant_ids:
        active_run = await MaterializationRun.objects.filter(
            tenant_schema__tenant_id__in=tenant_ids,
            state__in=list(MaterializationRun.ACTIVE_STATES),
        ).afirst()

    if active_run is not None or (vs is not None and vs.state == SchemaState.MATERIALIZING):
        if not interactive:
            return _HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE
        return (
            "A materialization is already in progress in the background. Do NOT "
            "trigger another one and do NOT call other data tools (the data is "
            "not yet ready). Briefly tell the user it's still loading and end "
            "your turn — the system will resume the conversation automatically "
            "when the current materialization completes."
        )

    if vs is None or vs.state != SchemaState.ACTIVE:
        if not interactive:
            return f"{_MULTI_TENANT_NAMESPACE_HINT}\n\n{_HEADLESS_MATERIALIZE_GUIDANCE}"
        return (
            f"{_MULTI_TENANT_NAMESPACE_HINT}\n\n"
            "No data has been loaded yet. Call `run_materialization` to start "
            "loading data for all tenants in this workspace. This tool returns "
            "IMMEDIATELY with `status: started` — do NOT call other data tools "
            "in the same turn. Acknowledge to the user in ONE sentence and end "
            "your turn. The system will resume the conversation automatically "
            "when materialization completes."
        )

    tables: list[dict] = []
    try:
        ctx = await load_workspace_context(str(workspace.id))
        tables = await workspace_list_tables(ctx)
    except Exception:
        logger.debug("Could not fetch multi-tenant table list for context injection", exc_info=True)

    last_run = None
    if tenant_ids:
        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema__tenant_id__in=tenant_ids,
                state__in=[
                    MaterializationRun.RunState.COMPLETED,
                    MaterializationRun.RunState.PARTIAL,
                ],
            )
            .order_by("-completed_at")
            .afirst()
        )
    last_materialized_at = (
        last_run.completed_at.isoformat() if last_run and last_run.completed_at else None
    )

    if not tables:
        return (
            f"{_MULTI_TENANT_NAMESPACE_HINT}\n\n"
            "Data is loaded but no tables are visible yet. The view schema may "
            "still be initializing — call `list_tables` to re-check shortly."
        )

    lines: list[str] = []
    if last_materialized_at:
        lines.append(f"Data is loaded and ready. Last updated: {last_materialized_at}")
    else:
        lines.append("Data is loaded and ready.")
    lines.append("")
    lines.append(_MULTI_TENANT_NAMESPACE_HINT)
    lines.append("")
    lines.append("### Available Tables")
    lines.append("")
    lines.append("| Table |")
    lines.append("|---|")
    for t in tables:
        lines.append(f"| {t['name']} |")
    lines.append("")
    lines.append("Use the `describe_table` tool for column details.")
    return "\n".join(lines)


def _llm_tool_schemas(tools: list, hidden_params: list[str]) -> list:
    """Build LLM tool definitions with the injected context-ID params omitted from
    the schema, so the LLM can't supply (and hallucinate) values that are injected
    from state. Non-MCP tools are returned unchanged.
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


def _build_cached_system_message(stable: str, volatile: str) -> SystemMessage:
    """Build a list-content SystemMessage with an Anthropic cache breakpoint.

    A ``cache_control`` breakpoint on the stable prefix's last block caches tool
    schemas + frozen system prefix together (langchain renders tools -> system ->
    messages; arch #254, finding 02#3). The volatile suffix follows WITHOUT a
    breakpoint, so a new materialization changes only post-breakpoint bytes and
    leaves the cached prefix intact.
    """
    blocks: list[dict] = [{"type": "text", "text": stable, "cache_control": PROMPT_CACHE_CONTROL}]
    if volatile and volatile.strip():
        blocks.append({"type": "text", "text": volatile})
    return SystemMessage(content=blocks)


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

    async def injecting_node(state: AgentState) -> dict[str, Any]:
        messages = list(state["messages"])
        last_msg = messages[-1]

        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            modified_msg = copy.copy(last_msg)
            modified_calls = []
            for tc in last_msg.tool_calls:
                if tc["name"] in MCP_TOOL_NAMES:
                    extra = {k: state.get(v, "") for k, v in injections.items()}
                    tc_id = tc.get("id") or ""
                    if not tc_id:
                        logger.warning(
                            "MCP tool call '%s' has no id; tool_call_id will be empty — "
                            "background-job attribution will fail",
                            tc["name"],
                        )
                    extra["tool_call_id"] = tc_id
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
            conversation (so shared/public thread pages can find them).
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

    tools = _build_tools(
        workspace,
        user,
        mcp_tools or [],
        conversation_id=conversation_id,
        interactive=interactive,
        job_id=job_id,
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
    )
    llm_tool_schemas = _llm_tool_schemas(tools, hidden_params=hidden_params)
    llm_with_tools = llm.bind_tools(llm_tool_schemas)

    stable_prompt, volatile_prompt = await _build_system_prompt(
        workspace, user, interactive=interactive
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

        messages = [_build_cached_system_message(stable_prompt, volatile_prompt), *repaired]
        # cache_control lands on the last eligible message block, caching the
        # (pruned) conversation-history prefix (arch #254, 02#3).
        response = await llm_with_tools.ainvoke(messages, cache_control=PROMPT_CACHE_CONTROL)
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        """Route to tools if the last message has tool calls, else end."""
        messages = state.get("messages", [])
        if not messages:
            return END

        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        return END

    def post_tools_router(state: AgentState) -> Literal["agent", "escalate"]:
        """Route post-tools: escalate if the agent is in a panic loop, else agent.

        See ``_should_escalate`` for the loop-detection rule. The escalation
        node ends the turn with a fixed message — no further tool calls.
        """
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
        return {"messages": [AIMessage(content=ESCALATION_MESSAGE)]}

    graph = StateGraph(AgentState)

    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("escalate", escalation_node)

    graph.set_entry_point("agent")

    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
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
) -> list:
    """Build the tool list: MCP data tools plus local artifact/recipe/learning
    tools, and a blocking materialization tool in headless mode.
    """
    # Drop MCP tools that must not reach the LLM (see AGENT_EXCLUDED_MCP_TOOLS).
    # In headless mode also drop the interactive fire-and-ack run_materialization
    # (needs a Thread + checkpointer + async resume a headless run lacks); it's
    # replaced below by the blocking materialize tool.
    excluded = set(AGENT_EXCLUDED_MCP_TOOLS)
    if not interactive:
        excluded.add("run_materialization")
    tools = [t for t in mcp_tools if getattr(t, "name", None) not in excluded]
    tools.append(create_save_learning_tool(workspace, user))
    tools.extend(create_artifact_tools(workspace, user, conversation_id=conversation_id))
    tools.append(create_recipe_tool(workspace, user))
    if not interactive:
        tools.append(create_materialization_tool(workspace, user, job_id))
    return tools


async def _build_system_prompt(
    workspace: Workspace, user, interactive: bool = True
) -> tuple[str, str]:
    """Assemble the workspace system prompt as a (stable, volatile) split.

    Stable = base prompt + artifact additions + workspace instructions +
    knowledge (rarely change; invalidated via the cache key below). Volatile =
    tenant context + ``## Data Availability`` (row counts / last-materialized
    timestamp, change every materialization).

    Splitting lets the agent node put the ``cache_control`` breakpoint on the
    stable prefix while the volatile block sits after it, so a new materialization
    no longer rewrites cached prefix bytes and defeats every cache hit (arch #254,
    finding 02#3). ``volatile_suffix`` may be "".
    """
    cache_key = _system_prompt_cache_key(workspace, user, interactive)
    cached = _system_prompt_cache.get(cache_key)
    if cached is not None:
        value, timestamp = cached
        if time.monotonic() - timestamp < _SYSTEM_PROMPT_TTL:
            return value

    # Stable sections (cacheable prefix)
    stable_sections = [BASE_SYSTEM_PROMPT, ARTIFACT_PROMPT_ADDITION]

    if workspace.system_prompt:
        stable_sections.append(f"\n## Workspace Instructions\n\n{workspace.system_prompt}\n")

    retriever = KnowledgeRetriever(workspace)
    knowledge_context = await retriever.retrieve()
    if knowledge_context:
        # Retriever already emits a ``## Knowledge Base`` heading; don't double it
        # (arch #254, finding 01#4).
        stable_sections.append(f"\n{knowledge_context}\n")

    # Volatile sections (after the cache breakpoint)
    volatile_sections: list[str] = []

    tenant_count = await workspace.tenants.acount()

    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        pipeline_config = get_registry().get_by_provider(tenant.provider)
        pipeline_name = pipeline_config.name if pipeline_config else "commcare_sync"

        volatile_sections.append(f"""
## Tenant Context

- Tenant: {tenant.canonical_name} ({tenant.external_id})
- Provider: {tenant.get_provider_display()}
- Pipeline: {pipeline_name}

## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")

        # Pre-fetch so the agent needn't call get_schema_status at runtime.
        schema_context = await _fetch_schema_context(tenant, user, interactive)
        volatile_sections.append(f"\n## Data Availability\n\n{schema_context}\n")
    elif tenant_count > 1:
        volatile_sections.append("""
## Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")
        schema_context = await _fetch_multi_tenant_schema_context(workspace, user, interactive)
        volatile_sections.append(f"\n## Data Availability\n\n{schema_context}\n")

    stable = "\n".join(stable_sections)
    volatile = "\n".join(volatile_sections)
    result = (stable, volatile)

    _system_prompt_cache[cache_key] = (result, time.monotonic())

    if len(_system_prompt_cache) > 256:
        now = time.monotonic()
        expired = [
            k for k, (_, ts) in _system_prompt_cache.items() if now - ts > _SYSTEM_PROMPT_TTL
        ]
        for k in expired:
            del _system_prompt_cache[k]

    return result


__all__ = [
    "ESCALATION_MESSAGE",
    "ESCALATION_TRIGGER_COUNT",
    "_should_escalate",
    "build_agent_graph",
]
