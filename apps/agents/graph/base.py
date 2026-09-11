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

from asgiref.sync import sync_to_async
from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from apps.agents.graph.state import AgentState, prune_messages
from apps.agents.prompts.artifact_prompt import ARTIFACT_PROMPT_ADDITION
from apps.agents.prompts.base_system import BASE_SYSTEM_PROMPT
from apps.agents.subagents.events import (
    SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    SUBAGENT_TOOL_NAMES,
    reset_subagent_event_queue,
    set_subagent_event_queue,
)
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.materialization_tool import create_materialization_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.knowledge.services.retriever import KnowledgeRetriever
from apps.semantic.services.catalog import (
    SemanticCatalogUnavailable,
    get_active_semantic_model,
)
from apps.workspaces.access import aresolve_workspace_access
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceRole,
    WorkspaceViewSchema,
)
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    select_pipeline_config,
)
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
        # workspace_id is injected into these so an LLM-supplied run_id is scoped
        # to the calling workspace (arch #253, 01#6).
        "get_materialization_status",
        "cancel_materialization",
    }
)

LOCAL_CONTEXT_TOOL_NAMES = frozenset({"artifact_manager", "canvas_manager"})

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
ARTIFACT_MANAGER_SYNTHETIC_TASK_MAX_CHARS = 2_000


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


def _system_prompt_cache_key(
    workspace,
    user,
    interactive: bool = True,
    canvas_write: bool = False,
) -> str:
    """Build a cache key from workspace + user properties that affect the prompt.

    Includes user.id conservatively: the reason given for it — that
    _fetch_schema_context scoped TenantMetadata per user — no longer holds
    (TenantMetadata is per tenant, #305, and that function does not read it at
    all), but sharing one prompt across a workspace's users needs its own audit.
    Includes workspace.system_prompt hash
    so edits invalidate immediately. Includes ``interactive`` because the
    materialization guidance differs between interactive (fire-and-resume) and
    headless (blocking) runs. Includes ``canvas_write`` because write-capable
    chats get different dataset-editing instructions from read-only chats.
    """
    prompt_hash = hashlib.md5(
        (workspace.system_prompt or "").encode(), usedforsecurity=False
    ).hexdigest()[:8]
    user_id = getattr(user, "id", "anon")
    mode = "i" if interactive else "h"
    canvas_mode = "cw" if canvas_write else "cr"
    return f"{workspace.id}:{user_id}:{prompt_hash}:{mode}:{canvas_mode}"


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


# Deliberately mode-agnostic: no "end your turn" (the headless runs have no
# resume path) and no retry hint, because no amount of waiting or re-running
# fixes a missing pipeline definition.
_PIPELINE_UNRESOLVED_GUIDANCE = (
    "This workspace's data cannot be described: Scout cannot determine which "
    "pipeline loaded it, which is a configuration error on Scout's side. Do NOT "
    "call data tools or `run_materialization` — they will fail the same way. "
    "Tell the user their workspace needs an administrator to configure its "
    "provider pipeline, and stop there."
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

    try:
        pipeline_config = select_pipeline_config(provider=tenant.provider)
    except PipelineResolutionError:
        # Guidance, not a raise: this builds the system prompt on every turn, so
        # raising would take the whole chat down. The detail stays in the log —
        # the agent only needs to know the workspace is not describable and that
        # no tool call will change that (#155).
        logger.exception("Pipeline resolution failed for the agent prompt")
        return _PIPELINE_UNRESOLVED_GUIDANCE

    # transformation-aware listing prefers terminal models over replaced ones
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


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _artifact_manager_task_is_missing(args: Any) -> bool:
    if not isinstance(args, dict):
        return True
    task = args.get("task")
    return not isinstance(task, str) or not task.strip()


def _latest_human_text(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            text = _message_text(msg).strip()
            if text:
                return text
    return ""


def _synthesize_artifact_manager_task(messages: list[Any]) -> str:
    """Build a bounded fallback task when the LLM emits an empty tool call.

    Anthropic can still produce an empty argument object despite a required
    schema. For artifact work, the user's latest request is enough context: the
    Artifact Manager subagent owns data discovery, semantic query verification,
    graph construction, and validation.
    """

    user_request = _latest_human_text(messages)
    if len(user_request) > ARTIFACT_MANAGER_SYNTHETIC_TASK_MAX_CHARS:
        user_request = user_request[:ARTIFACT_MANAGER_SYNTHETIC_TASK_MAX_CHARS].rstrip()
        user_request += "..."
    if not user_request:
        user_request = "Create or update the semantic story artifact requested in this thread."

    return (
        "Create or update a semantic story artifact for the user's latest request. "
        "Treat this as a complete delegated artifact task. Do your own dataset "
        "discovery and semantic-query verification inside the Artifact Manager; "
        "use live semantic data, hidden semantic_query blocks, validated graph/table/stat "
        "bindings, and publish only after artifact_write validation succeeds.\n\n"
        f"User request:\n{user_request}"
    )


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
            result = await base_tool_node.ainvoke({"messages": messages}, config=config)
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

    # Same policy as the canvas REST endpoints: only members above the read
    # role can stage/commit canvas changes, so read-only members never get the
    # canvas_manager tool (the tool closures re-check as the hard boundary).
    canvas_membership = None
    if interactive and conversation_id and user is not None:
        _authorized_workspace, canvas_membership = await aresolve_workspace_access(
            user, workspace.id
        )
    canvas_write = bool(
        canvas_membership is not None and canvas_membership.role != WorkspaceRole.READ
    )

    # --- Build tools ---
    tools = _build_tools(
        workspace,
        user,
        mcp_tools or [],
        conversation_id=conversation_id,
        interactive=interactive,
        job_id=job_id,
        canvas_write=canvas_write,
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
        workspace,
        user,
        interactive=interactive,
        canvas_write=canvas_write,
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
    canvas_write: bool = False,
) -> list:
    """Build the tool list: MCP data tools plus local artifact/recipe/learning
    tools, and a blocking materialization tool in headless mode.
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
    from apps.agents.tools.canvas_manager_agent import create_canvas_manager_tool
    from apps.agents.tools.canvas_tool import create_canvas_read_tool

    tools.append(create_save_learning_tool(workspace, user))
    tools.append(
        create_artifact_manager_tool(
            workspace,
            user,
            mcp_tools or [],
            conversation_id=conversation_id,
        )
    )
    if interactive and conversation_id:
        # The canvas is thread-bound; headless (recipe) runs have no thread.
        # The parent keeps a read-only canvas_read for cheap draft questions;
        # all canvas writes are delegated to the Canvas Manager subagent,
        # which read-only workspace members do not get at all.
        tools.append(create_canvas_read_tool(workspace, user, conversation_id))
        if canvas_write:
            tools.append(
                create_canvas_manager_tool(
                    workspace,
                    user,
                    mcp_tools or [],
                    conversation_id=conversation_id,
                )
            )
    tools.append(create_recipe_tool(workspace, user))
    if not interactive:
        tools.append(create_materialization_tool(workspace, user, job_id))
    return tools


async def _build_system_prompt(
    workspace: Workspace,
    user,
    interactive: bool = True,
    canvas_write: bool = False,
) -> tuple[str, str]:
    """Assemble the workspace system prompt as a (stable, volatile) split.

    Stable = base prompt + artifact additions + workspace instructions +
    knowledge + dataset/canvas guidance. Volatile = runtime data availability,
    which may change after every materialization.

    Splitting lets the agent node put the ``cache_control`` breakpoint on the
    stable prefix while the volatile block sits after it, so a new materialization
    no longer rewrites cached prefix bytes and defeats every cache hit (arch #254,
    finding 02#3). ``volatile_suffix`` may be "".
    """
    cache_key = _system_prompt_cache_key(workspace, user, interactive, canvas_write)
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

    if tenant_count > 0:
        stable_sections.append("""
## Workspace And Dataset Discovery

Semantic datasets are the primary objects users see and ask about. A dataset
has business-facing dimensions, time dimensions, measures, relationships,
labels, descriptions, and display metadata. Workspace names, providers,
pipelines, and dataset lists are runtime data; do not assume they are present
in the system prompt.

Use dataset tools by intent:
- Discover available data: `list_workspaces` and `list_datasets`.
- Inspect one dataset's fields, labels, descriptions, formats, and
  relationships: `describe_dataset`.
- Answer analytical questions: `semantic_query` over semantic members only.
- Change dataset definitions, labels, descriptions, fields, relationships, or
  display metadata: use the Semantic Canvas instructions below when available;
  do not invent raw SQL or mutate datasets directly from the parent chat agent.

Dataset editing vocabulary:
- Create a new dataset: ask the canvas manager to create a CTE/SQL-derived
  dataset with `definition_sql` and a `primary_key`.
- Add fields: ask the canvas manager to add dimensions or measures with field
  names, source columns, and measure types.
- Change a value format / number format / display format: ask the canvas
  manager to set field metadata `format` and optional `currency`. Examples:
  `number_0`, `number_2`, `percent_1`, `currency_2` + `USD`,
  `accounting_2`. If the user says `decimal_02`, use `number_2`.

## Semantic Query Configuration

- Maximum rows per query: 500
- Query timeout: 30 seconds

When results are truncated, suggest adding filters or using aggregations to reduce the result size.
""")

        semantic_context = await _fetch_semantic_model_context(workspace, interactive)
        volatile_sections.append(f"\n## Data Availability\n\n{semantic_context}\n")

    if interactive and canvas_write:
        stable_sections.append("""
## Semantic Canvas (dataset editing)

This conversation has a canvas — a draft changeset over the workspace's
datasets, shown to the user in the side panel. When the user asks to edit a
dataset's label/description, field labels/descriptions, value format / display
format / number format or currency, add measures or dimensions, link datasets, or create a
CTE/SQL-derived dataset, delegate the whole job to the `canvas_manager` tool
with a complete task description (datasets, field names and source columns,
relationship endpoints, SQL, display metadata, and whether to commit). Use
`canvas_read` yourself only to answer questions about pending draft changes.
Draft changes become queryable only after the canvas is committed.
Autogenerated fields can be curated (label/description/format/currency) but
never removed or retyped.
For value format requests, tell `canvas_manager` to set field metadata `format`
and optional `currency`.
""")
    elif interactive:
        stable_sections.append("""
## Semantic Canvas (read-only)

This conversation has a canvas showing draft dataset changes in the side
panel. Use `canvas_read` to answer questions about it. This user's workspace
role is read-only: you cannot stage or save dataset changes for them — if they
ask to update dataset labels, descriptions, fields, relationships, formats, or
currency, explain that a read-write workspace role is required.
""")

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
