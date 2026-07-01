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
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from asgiref.sync import sync_to_async
from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.state import AgentState
from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from apps.agents.subagents.events import (
    SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    reset_subagent_event_queue,
    set_subagent_event_queue,
)
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.materialization_tool import create_materialization_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.knowledge.services.retriever import KnowledgeRetriever
from apps.semantic.services.catalog import (
    SemanticCatalogUnavailable,
    get_active_semantic_model,
)
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceViewSchema,
)
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.metadata import (
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
        "get_metadata",
        "list_workspaces",
        "list_datasets",
        "semantic_catalog",
        "describe_dataset",
        "semantic_query",
        "run_materialization",
        "get_schema_status",
        "get_lineage",
    }
)

LOCAL_CONTEXT_TOOL_NAMES = frozenset({"artifact_manager"})

# MCP tools the server advertises but that must NEVER be exposed to the agent.
#
# ``teardown_schema`` (arch #237 / finding 00#2) physically DROPs every tenant
# and view schema for a workspace but updates no Django state — TenantSchema
# stays ACTIVE over dropped schemas, MaterializationRuns stay COMPLETED, the
# WorkspaceViewSchema stays ACTIVE, and sibling multi-tenant workspaces sharing
# the (external_id-keyed) tenant schema are silently destroyed without being
# failed. Its only guards are an LLM-suppliable ``confirm`` flag and workspace
# existence; there is no role/membership check. It duplicates the worker
# ``teardown_schema`` task (which carries the full state-update + sibling-fail
# machinery) with none of its safety, and has no legitimate agent use case
# (schemas are re-provisioned automatically on the next materialization). It is
# therefore filtered out before tools are bound to the LLM. The MCP server still
# defines the tool so operator/HTTP callers are unaffected.
AGENT_EXCLUDED_MCP_TOOLS = frozenset(
    {
        "teardown_schema",
        # Semantic-model mode: keep raw table-inspection tools server-side for
        # internal and operator callers, but do not expose them to the LLM.
        "list_tables",
        "describe_table",
        "get_metadata",
    }
)

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
    }
)


DEFAULT_MAX_TOKENS = 4096

# Circuit-breaker thresholds for the escalation node. If the last N tool
# messages all carry one of these error codes, the agent has drifted from
# self-correction into a panic loop — route to the escalation node so the
# turn ends with an explicit ask instead of consuming the recursion budget.
#
# These are the bare ``error.code`` values from the MCP envelope
# (mcp_server.envelope), matched against the parsed JSON ``error.code`` field —
# NOT a substring search. The previous implementation substring-matched
# ``'"code": "NOT_FOUND"'`` (with a space after the colon), which only worked
# because FastMCP serialized with ``indent=2``; a switch to compact separators
# would have silently disabled the breaker (06#1). They are also the single
# source of truth shared with the base system prompt's "When the Schema is
# Broken" rule (see apps/agents/prompts/base_system.py).
ESCALATION_ERROR_CODES = frozenset({"NOT_FOUND", "VALIDATION_ERROR"})
ESCALATION_TRIGGER_COUNT = 3


def _tool_message_error_code(content: Any) -> str | None:
    """Extract the MCP envelope ``error.code`` from a ToolMessage's content.

    Tool content may be a JSON string, a list of content blocks (the shape
    langchain_mcp_adapters emits), or already-parsed structures. We parse the
    JSON and read ``error.code`` so the panic-loop detector keys off the
    structured field rather than a whitespace-sensitive substring (06#1).
    Returns None when the content isn't a recognizable error envelope.
    """
    if isinstance(content, list):
        # Content-block list: try each text block until one parses to an error.
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
    """Detect a panic loop: last N tool messages all returned an error code.

    Looks only at trailing ``ToolMessage``s — a successful tool call in
    between resets the streak. Parses each tool message's JSON envelope and
    matches the structured ``error.code`` value against
    ``ESCALATION_ERROR_CODES`` (06#1) rather than substring-searching the raw
    text, so a serialization/whitespace change can't silently disable the
    breaker and an unrelated row containing the literal "NOT_FOUND" can't
    falsely trip it.
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


# Simple TTL cache for system prompts
_system_prompt_cache: dict[str, tuple[str, float]] = {}
_SYSTEM_PROMPT_TTL = 60  # 60 seconds — short to limit staleness from knowledge/schema changes


def _system_prompt_cache_key(workspace, user, interactive: bool = True) -> str:
    """Build a cache key from workspace + user properties that affect the prompt.

    Includes user.id because _fetch_schema_context scopes TenantMetadata
    lookup to the specific user. Includes workspace.system_prompt hash
    so edits invalidate immediately. Includes ``interactive`` because the
    materialization guidance differs between interactive (fire-and-resume) and
    headless (blocking) runs — caching one for the other would mislead the agent.
    """
    prompt_hash = hashlib.md5(
        (workspace.system_prompt or "").encode(), usedforsecurity=False
    ).hexdigest()[:8]
    user_id = getattr(user, "id", "anon")
    mode = "i" if interactive else "h"
    return f"{workspace.id}:{user_id}:{prompt_hash}:{mode}"


def _semantic_catalog_context_sync(workspace) -> str:
    get_active_semantic_model(workspace)
    return (
        "Data is loaded and ready through the workspace semantic model. "
        "Use `list_workspaces` to inspect accessible workspaces, `list_datasets` "
        "to page through dataset summaries, `describe_dataset` for one dataset's "
        "members, and `semantic_query` for analysis. Do not write SQL."
    )


async def _fetch_semantic_model_context(workspace, interactive: bool = True) -> str:
    try:
        return await sync_to_async(_semantic_catalog_context_sync, thread_sensitive=True)(workspace)
    except SemanticCatalogUnavailable:
        tenant_count = await workspace.tenants.acount()
        if tenant_count == 1:
            tenant = await workspace.tenants.afirst()
            ts = await TenantSchema.objects.filter(
                tenant=tenant,
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            ).afirst()
            if ts is None:
                return (
                    _HEADLESS_MATERIALIZE_GUIDANCE
                    if not interactive
                    else (
                        "No data has been loaded yet. Call `run_materialization` to start "
                        "loading. This tool returns IMMEDIATELY with `status: started` — do "
                        "NOT call other data tools in the same turn. Acknowledge to the user "
                        "in ONE sentence and end your turn. The system will resume the "
                        "conversation automatically when materialization completes."
                    )
                )
            if ts.state == SchemaState.MATERIALIZING:
                return (
                    _HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE
                    if not interactive
                    else (
                        "A materialization is already in progress in the background. Do NOT "
                        "trigger another one and do NOT call other data tools. Briefly tell "
                        "the user it's still loading and end your turn — the system will "
                        "resume the conversation automatically when materialization completes."
                    )
                )
            return (
                "Data is loaded, but no semantic datasets are available yet. "
                "Run materialization to rebuild the semantic catalog, then use "
                "`list_datasets` and `semantic_query`."
            )
        if tenant_count > 1:
            vs = await WorkspaceViewSchema.objects.filter(workspace_id=workspace.id).afirst()
            if vs is not None and vs.state == SchemaState.MATERIALIZING:
                return (
                    _HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE
                    if not interactive
                    else (
                        "A materialization is already in progress in the background. Do NOT "
                        "trigger another one and do NOT call other data tools. Briefly tell "
                        "the user it's still loading and end your turn — the system will "
                        "resume the conversation automatically when materialization completes."
                    )
                )
            return (
                f"{_MULTI_TENANT_NAMESPACE_HINT}\n\n"
                "No semantic datasets are available yet. Call `run_materialization` "
                "to load workspace data and rebuild the semantic catalog."
            )
        return (
            _HEADLESS_MATERIALIZE_GUIDANCE
            if not interactive
            else (
                "No data has been loaded yet. Call `run_materialization` to start "
                "loading. This tool returns IMMEDIATELY with `status: started` — do "
                "NOT call other data tools in the same turn. Acknowledge to the user "
                "in ONE sentence and end your turn. The system will resume the "
                "conversation automatically when materialization completes."
            )
        )


# Guidance injected for HEADLESS (non-interactive) runs — e.g. recipe execution.
# Such runs have no chat Thread/checkpointer and no async-resume path, so the
# agent must NOT "end its turn and wait": the headless run_materialization tool
# BLOCKS and returns when loading finishes, and the agent continues in the same
# run. (Deliberately avoids the substring "end your turn".)
_HEADLESS_MATERIALIZE_GUIDANCE = (
    "No data has been loaded yet. Call `run_materialization` to load it. This "
    "tool BLOCKS and returns a status summary once loading finishes — keep "
    "working in the same run rather than stopping. After it returns "
    "`status: completed`, continue with the requested analysis; the data is ready."
)

# Headless guidance when a materialization is ALREADY in progress. The headless
# `run_materialization` tool waits for the in-flight load rather than starting a
# parallel one, so we still route the agent to it — but we must NOT say "no data
# loaded" (that would read as "start one"), matching the interactive path's
# "don't trigger another" intent.
_HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE = (
    "A data load is already in progress for this workspace. Call "
    "`run_materialization` to ensure fresh data — it WAITS for the in-progress "
    "load to finish (it does not start a parallel one) and returns when the data "
    "is ready. Then continue with the requested analysis in the same run."
)


async def _fetch_schema_context(tenant, user, interactive: bool = True) -> str:
    """Fetch database schema state and build a ## Data Availability prompt section.

    Reports availability state without embedding table or dataset names. Runtime
    dataset discovery belongs in MCP tools such as ``list_datasets``.

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
        # NB: no `pipeline=` argument — run_materialization takes no pipeline
        # parameter (routing moved into materialize_workspace per-provider) and
        # all of its real params are injected server-side and hidden, so the
        # LLM-facing schema is empty. Emitting `pipeline="..."` here told the
        # agent to send an argument the tool can't accept (finding 02#6).
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
        return (
            "Data is loaded but no semantic datasets are available yet. The "
            "materialization may still be completing. Retry `list_datasets` shortly."
        )

    last_materialized_at = tables[0].get("materialized_at") if tables else None
    if last_materialized_at:
        loaded = f"Data is loaded and ready. Last updated: {last_materialized_at}."
    else:
        loaded = "Data is loaded and ready."
    compact = (
        f"{loaded} Use `list_datasets` to page through dataset summaries, "
        "`describe_dataset` for one dataset's members, and `semantic_query` "
        "for analysis."
    )
    if terminal_assets:
        compact += (
            "\n\nThese tables are produced by a transformation pipeline. "
            "Use the `get_lineage` tool to explore how any table was built."
        )
    return compact


_MULTI_TENANT_NAMESPACE_HINT = (
    "This is a multi-tenant workspace. Use the semantic catalog rather than "
    "raw tenant tables; semantic datasets handle the workspace scope."
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

    lines: list[str] = []
    if last_materialized_at:
        lines.append(f"Data is loaded and ready. Last updated: {last_materialized_at}")
    else:
        lines.append("Data is loaded and ready.")
    lines.append("")
    lines.append(_MULTI_TENANT_NAMESPACE_HINT)
    lines.append("")
    lines.append("Use `list_datasets` and `describe_dataset` for dataset details.")
    return "\n".join(lines)


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
    """Create a graph node that injects state values into MCP tool call args.

    Before the ToolNode executes, this node copies the last AI message and
    injects values from the agent state into every MCP tool call's args.
    ``injections`` maps tool-arg-name → state-field-name.  This ensures the
    MCP server always receives the correct context IDs regardless of what the
    LLM generated.
    """

    async def injecting_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        messages = list(state["messages"])
        last_msg = messages[-1]
        event_queue = None
        if isinstance(config, dict):
            event_queue = (config.get("configurable") or {}).get(
                SUBAGENT_EVENT_QUEUE_CONFIG_KEY
            )

        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            modified_msg = copy.copy(last_msg)
            modified_calls = []
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
                elif tc["name"] in LOCAL_CONTEXT_TOOL_NAMES:
                    extra = {"tool_call_id": tc_id}
                    if tc["name"] == "artifact_manager":
                        extra[SUBAGENT_EVENT_QUEUE_CONFIG_KEY] = event_queue
                    tc = {**tc, "args": {**tc["args"], **extra}}
                modified_calls.append(tc)
            modified_msg.tool_calls = modified_calls
            messages = [*messages[:-1], modified_msg]

        token = set_subagent_event_queue(event_queue)
        try:
            return await base_tool_node.ainvoke({"messages": messages}, config=config)
        finally:
            reset_subagent_event_queue(token)

    injecting_node.__annotations__["config"] = RunnableConfig | None
    return injecting_node


async def build_agent_graph(
    workspace: Workspace,
    user: User | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    mcp_tools: list | None = None,
    oauth_tokens: dict | None = None,
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
        oauth_tokens: Optional OAuth tokens for tool authentication.
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

    # --- Build tools ---
    tools = _build_tools(
        workspace,
        user,
        mcp_tools or [],
        conversation_id=conversation_id,
        interactive=interactive,
        job_id=job_id,
    )
    logger.debug("Created %d tools for workspace %s", len(tools), workspace.id)

    # --- Inject workspace_id and user_id into MCP tool calls from agent state ---
    injections = {
        "workspace_id": "workspace_id",
        "user_id": "user_id",
        "thread_id": "thread_id",
    }
    # tool_call_id is injected per-call (from the LangChain tool_call dict's
    # own id), not from agent state, so it can't live in `injections`.
    # INJECTED_TOOL_PARAMS is the single source of truth for the LLM-facing
    # hidden set (also used to redact tool input in the SSE stream).
    hidden_params = list(INJECTED_TOOL_PARAMS)

    # --- Build LLM with tools ---
    # Opus 4.7+ removed the sampling params (temperature/top_p/top_k);
    # sending any of them returns a 400.
    llm = ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
    )
    llm_tool_schemas = _llm_tool_schemas(tools, hidden_params=hidden_params)
    llm_with_tools = llm.bind_tools(llm_tool_schemas)

    # --- Build system prompt ---
    system_prompt = await _build_system_prompt(workspace, user, interactive=interactive)
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

    # --- Build the graph ---
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("escalate", escalation_node)

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

    # tools -> agent (normal) or -> escalate (panic loop detected, terminal)
    graph.add_conditional_edges(
        "tools",
        post_tools_router,
        {
            "agent": "agent",
            "escalate": "escalate",
        },
    )
    graph.add_edge("escalate", END)

    # --- Compile and return ---
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
    """
    Build the tool list for the agent.

    MCP tools (from the Scout MCP server):
    - list_workspaces: List accessible workspaces and tenant/profile metadata
    - list_datasets: Page through business-facing datasets across workspaces
    - describe_dataset: Get dataset dimensions, measures, and relationships
    - semantic_query: Run structured measure/dimension queries

    Local tools (always included):
    - save_learning: For persisting discovered corrections
    - artifact_manager: For semantic graph/story artifact work
    - create_artifact/update_artifact: For legacy non-story artifacts
    - save_as_recipe: For creating replayable analysis workflows

    Args:
        workspace: The Workspace model instance.
        user: Optional User for tracking learning discovery.
        mcp_tools: LangChain tools loaded from the MCP server.
        conversation_id: Optional thread/conversation id, recorded on artifacts
            the agent creates so shared/public thread pages can find them.

    Returns:
        List of LangChain tool functions.
    """
    # Drop any MCP tool the server advertises but that must not reach the LLM
    # (e.g. the destructive ``teardown_schema`` — see AGENT_EXCLUDED_MCP_TOOLS).
    # In headless mode also drop the interactive fire-and-ack
    # ``run_materialization``: it requires a real chat Thread + checkpointer +
    # async resume that a headless run does not have. It is replaced below by the
    # blocking materialize tool, which runs the pipeline inline and returns when
    # data is ready.
    excluded = set(AGENT_EXCLUDED_MCP_TOOLS)
    if not interactive:
        excluded.add("run_materialization")
    tools = [t for t in mcp_tools if getattr(t, "name", None) not in excluded]
    from apps.agents.tools.artifact_manager_agent import create_artifact_manager_tool

    tools.append(create_save_learning_tool(workspace, user))
    tools.append(
        create_artifact_manager_tool(
            workspace,
            user,
            mcp_tools or [],
            conversation_id=conversation_id,
        )
    )
    tools.extend(create_artifact_tools(workspace, user, conversation_id=conversation_id))
    tools.append(create_recipe_tool(workspace, user))
    if not interactive:
        tools.append(create_materialization_tool(workspace, user, job_id))
    return tools


async def _build_system_prompt(workspace: Workspace, user, interactive: bool = True) -> str:
    """
    Assemble the complete system prompt for a workspace.

    The prompt is built from:
    1. BASE_SYSTEM_PROMPT: Core agent behavior and formatting
    2. ARTIFACT_PROMPT_ADDITION: Instructions for creating artifacts
    3. Workspace system prompt: Workspace-specific instructions
    4. Knowledge retriever output: Metrics, rules, learnings
    5. Runtime discovery guidance and query config

    Args:
        workspace: The Workspace model instance.
        user: The User model instance (used to scope tenant metadata lookup).

    Returns:
        Complete system prompt string.
    """
    cache_key = _system_prompt_cache_key(workspace, user, interactive)
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

    if tenant_count > 0:
        sections.append("""
## Workspace And Dataset Discovery

Workspace names, providers, pipelines, and dataset lists are runtime data.
Do not assume they are present in the system prompt. Use `list_workspaces`
and `list_datasets` when you need to know what is available.

## Semantic Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")

        semantic_context = await _fetch_semantic_model_context(workspace, interactive)
        sections.append(f"\n## Data Availability\n\n{semantic_context}\n")

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
    "ESCALATION_MESSAGE",
    "ESCALATION_TRIGGER_COUNT",
    "_should_escalate",
    "build_agent_graph",
]
