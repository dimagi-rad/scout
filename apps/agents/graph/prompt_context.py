"""System prompt assembly for the main agent graph.

Builds the (stable, volatile) system prompt split: the cacheable prefix (base
prompt, artifact additions, workspace instructions, knowledge, dataset and canvas
guidance) and the per-turn suffix (data availability, tenant coverage and source
freshness).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from langchain_core.messages import SystemMessage

from apps.agents.prompts.artifact_prompt import (
    ARTIFACT_PROMPT_ADDITION,
    ARTIFACT_READ_ONLY_PROMPT_ADDITION,
)
from apps.agents.prompts.base_system import select_base_system_prompt
from apps.agents.prompts.memory_prompt import MEMORY_GUIDANCE
from apps.common.identifiers import view_name
from apps.knowledge.services.retriever import KnowledgeRetriever
from apps.memory.services import apersonal_memory_prompt, has_personal_memory, prompt_hash
from apps.semantic.services.catalog import SemanticCatalogUnavailable, aget_active_semantic_model
from apps.workspaces.models import SchemaState, WorkspaceDataRecovery, WorkspaceViewSchema
from apps.workspaces.services.load_activity import (
    active_runs_for_workspaces,
    athread_awaits_load,
    aworkspace_schema_status,
)
from apps.workspaces.services.query_state import serving_writer_in_flight, workspace_query_surface
from apps.workspaces.services.source_freshness import (
    CREDENTIAL_CODES,
    REFRESHED,
    REUSED,
    aworkspace_source_freshness,
    provider_label,
)
from apps.workspaces.services.tenant_coverage import coverage_warning, parse_coverage
from mcp_server.pipeline_registry import get_registry

if TYPE_CHECKING:
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

# Anthropic prompt-caching breakpoint (arch #254, finding 02#3).
# Default 5-min ephemeral TTL breaks even at ~2 reads, which a single agent turn
# (K+1 LLM calls sharing one prefix) clears immediately. A 1h TTL
# ({"type": "ephemeral", "ttl": "1h"}) costs 2x to write / ~3 reads to pay off —
# use only for bursty traffic with multi-minute idle gaps. langchain-anthropic
# renders tools -> system -> messages, so a breakpoint on the last system block
# caches tool schemas + frozen system prefix together; a second breakpoint via
# the ``cache_control`` kwarg to ``ainvoke`` caches the replayed history.
PROMPT_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}


_system_prompt_cache: dict[str, tuple[str, float]] = {}
_SYSTEM_PROMPT_TTL = 60  # short, to limit staleness from knowledge/schema changes


def _system_prompt_cache_key(
    workspace,
    user,
    interactive: bool = True,
    canvas_write: bool = False,
    write_capable: bool = False,
    personal_memory: str = "",
) -> str:
    """Build a cache key from workspace + user properties that affect the prompt.

    Includes user.id conservatively: the original reason — per-user
    TenantMetadata in the prompt — no longer holds (TenantMetadata is per
    tenant, #305, and the prompt does not read it), but sharing one prompt
    across a workspace's users needs its own audit.
    Includes workspace.system_prompt hash
    so edits invalidate immediately. Includes ``interactive`` because the
    materialization guidance differs between interactive (fire-and-resume) and
    headless (blocking) runs. Includes ``canvas_write`` because write-capable
    chats get different dataset-editing instructions from read-only chats.
    Includes ``write_capable`` because it selects the artifact prompt and the
    read-only or write-capable materialization guidance. Includes a hash of the
    rendered personal memory so a saved, edited or deleted memory applies on the
    user's next message rather than after the TTL.
    """
    instructions_hash = prompt_hash(workspace.system_prompt or "")
    user_id = getattr(user, "id", "anon")
    mode = "i" if interactive else "h"
    canvas_mode = "cw" if canvas_write else "cr"
    tool_mode = "rw" if write_capable else "ro"
    memory_hash = prompt_hash(personal_memory)
    return (
        f"{workspace.id}:{user_id}:{instructions_hash}:{mode}:{canvas_mode}:{tool_mode}:"
        f"{memory_hash}"
    )


async def _semantic_catalog_context(workspace) -> str:
    await aget_active_semantic_model(workspace)
    return (
        "Data is loaded and ready through the workspace semantic model. "
        "Use `list_workspaces` to inspect accessible workspaces, `list_datasets` "
        "to page through this workspace's dataset summaries (pass `workspace_ids` "
        "only to look at other workspaces, whose data this chat cannot query), "
        "`describe_dataset` for one dataset's "
        "members, and `semantic_query` for analysis. When the semantic model "
        "cannot express the question, fall back to `list_tables`, "
        "`describe_table`, and read-only `query` SQL."
    )


async def _fetch_semantic_model_context(
    workspace,
    interactive: bool = True,
    write_capable: bool = False,
    *,
    conversation_id: str | None = None,
) -> str:
    # Age alone cannot prove a writer has stopped; the reconciler owns dead-run detection.
    # Runs track live work even while the previous semantic catalog remains active.
    active_runs = active_runs_for_workspaces([workspace.id])
    if await active_runs.aexists():
        tenant_count = await workspace.tenants.acount()
        serving_view = (
            await WorkspaceViewSchema.objects.filter(
                workspace=workspace, state=SchemaState.ACTIVE
            ).afirst()
            if tenant_count > 1
            else None
        )
        coverage = parse_coverage(serving_view.tenant_coverage) if serving_view else None
        unsafe_run = await serving_writer_in_flight(active_runs, coverage)
        if not unsafe_run:
            try:
                ready_context = await _semantic_catalog_context(workspace)
            except SemanticCatalogUnavailable:
                pass
            else:
                if tenant_count == 1 or serving_view is not None:
                    return (
                        "A refresh is in progress outside the currently serving data. "
                        "You may query the "
                        "previously loaded data while it finishes; tell the user results do "
                        "not include this refresh yet. Do NOT trigger another materialization."
                        f"{await _refresh_follow_up(interactive, conversation_id)}\n\n"
                        f"{ready_context}"
                    )
        return await _load_in_progress_guidance(interactive, write_capable, conversation_id)

    try:
        return await _semantic_catalog_context(workspace)
    except SemanticCatalogUnavailable:
        schema_status = await aworkspace_schema_status(workspace.id)
        loaded = schema_status == "available"
        multi = await workspace.tenants.acount() > 1
        unresolved, every = await _unresolved_pipeline_providers(workspace)
        # Waiting can't help either: no load finishes without a pipeline (G12).
        if every:
            guidance = _pipeline_unresolved_guidance(
                unresolved, loaded=loaded, write_capable=write_capable
            )
        elif schema_status == "provisioning":
            # Queued but not yet started, so no run above says so (#408).
            guidance = await _load_in_progress_guidance(interactive, write_capable, conversation_id)
        elif loaded and write_capable and interactive:
            guidance = await _semantic_rebuild_guidance(
                workspace, interactive, write_capable, conversation_id
            )
        else:
            guidance = _load_state_guidance(
                loaded=loaded, interactive=interactive, write_capable=write_capable
            )
        if not every and unresolved:
            guidance = f"{guidance}\n\n{_partial_pipeline_note(unresolved)}"
        return f"{_MULTI_TENANT_NAMESPACE_HINT}\n\n{guidance}" if multi else guidance


async def _refresh_follow_up(interactive: bool, conversation_id: str | None) -> str:
    # A chat that started this refresh keeps its PENDING ThreadJob while the old data
    # still serves, so it must hear the same resume promise as the bound load path.
    if not interactive:
        return ""
    if conversation_id and await athread_awaits_load(conversation_id):
        return " This conversation will resume automatically when the refresh finishes."
    return (
        " Nothing will resume this conversation when the refresh finishes; if the user "
        "wants refreshed results, tell them to ask again once it has, without naming a time."
    )


async def _load_in_progress_guidance(
    interactive: bool, write_capable: bool, conversation_id: str | None
) -> str:
    if not write_capable:
        return _READ_ONLY_MATERIALIZE_IN_PROGRESS_GUIDANCE
    if not interactive:
        return _HEADLESS_MATERIALIZE_IN_PROGRESS_GUIDANCE
    if conversation_id and await athread_awaits_load(conversation_id):
        return _LOAD_STARTED_FOR_THIS_CHAT_GUIDANCE
    return _INTERACTIVE_MATERIALIZE_IN_PROGRESS_GUIDANCE


async def _semantic_rebuild_guidance(
    workspace, interactive: bool, write_capable: bool, conversation_id: str | None
) -> str:
    """Loaded data with no catalog needs a rebuild, which the chat starts itself (#714).

    Only a snapshot a failed load left unsafe needs a reload; the agent must never
    ask to approve one just to get the data model.
    """
    active = await (
        WorkspaceDataRecovery.objects.filter(
            workspace=workspace, state__in=list(WorkspaceDataRecovery.ACTIVE_STATES)
        )
        .values_list("recovery_type", flat=True)
        .afirst()
    )
    if active == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION:
        return await _load_in_progress_guidance(interactive, write_capable, conversation_id)
    if active is not None:
        return _SEMANTIC_REBUILDING_GUIDANCE
    surface = await workspace_query_surface(workspace)
    if surface["recovery_action"] == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION:
        return _LOADED_NEEDS_RELOAD_GUIDANCE
    return _SEMANTIC_REBUILD_NOT_RUNNING_GUIDANCE


def _load_state_guidance(*, loaded: bool, interactive: bool, write_capable: bool) -> str:
    if loaded:
        return (
            _HEADLESS_LOADED_REBUILD_GUIDANCE if write_capable else _READ_ONLY_LOADED_SQL_GUIDANCE
        )
    if not write_capable:
        return _READ_ONLY_MATERIALIZE_GUIDANCE
    return _INTERACTIVE_MATERIALIZE_GUIDANCE if interactive else _HEADLESS_MATERIALIZE_GUIDANCE


async def _unresolved_pipeline_providers(workspace) -> tuple[list[str], bool]:
    """Providers a load would fail ``PIPELINE_UNRESOLVED`` for, and whether that is all of them.

    Checked the way a load picks its pipeline: by provider alone.
    """
    # order_by(): Tenant's default ordering would otherwise leak into DISTINCT.
    providers = {
        provider
        async for provider in workspace.tenants.order_by()
        .values_list("provider", flat=True)
        .distinct()
    }
    unresolved = sorted(providers - {config.provider for config in get_registry().list()})
    if unresolved:
        # The agent no longer starts the run that would fail, so record it here.
        logger.warning("No materialization pipeline for provider(s) %s", ", ".join(unresolved))
    return unresolved, bool(providers) and len(unresolved) == len(providers)


def _pipeline_admin_advice(providers: list[str]) -> str:
    return (
        "an administrator must configure or repair the materialization pipeline for "
        f"provider {', '.join(providers)}; re-running materialization cannot fix this "
        "until that configuration changes"
    )


def _pipeline_unresolved_guidance(
    providers: list[str], *, loaded: bool, write_capable: bool
) -> str:
    """Mode-agnostic: no retry or wait fixes a missing pipeline definition (F2)."""
    no_rerun = " Do NOT call `run_materialization`; it fails the same way." if write_capable else ""
    if loaded:
        return (
            "Data is loaded, but no semantic datasets are available and materialization "
            "cannot rebuild them. Read-only `query` SQL still works on the loaded tables; "
            "`list_tables` and `describe_table` may fail for these sources."
            f"{no_rerun} If the user asks for semantic datasets or a refresh, tell them "
            f"{_pipeline_admin_advice(providers)}."
        )
    return (
        f"No data has been loaded, and materialization cannot load it.{no_rerun} "
        f"Tell the user {_pipeline_admin_advice(providers)}, and stop there."
    )


def _partial_pipeline_note(providers: list[str]) -> str:
    return (
        f"A load skips this workspace's provider {', '.join(providers)} sources: "
        f"{_pipeline_admin_advice(providers)}. Tell the user when it affects their question."
    )


# No `pipeline=` arg: run_materialization's LLM-facing schema is empty (all params
# injected server-side); naming an argument it can't accept confused the agent (02#6).
_INTERACTIVE_MATERIALIZE_GUIDANCE = (
    "No data has been loaded yet, and no load is running. Call `run_materialization` "
    "yourself to start "
    "loading; do not ask the user to start it. This tool returns IMMEDIATELY "
    "— do NOT call other data tools in the same turn. Tell the user in ONE "
    "sentence what its result says and end your turn. Say you will continue "
    "only if that result says this conversation will resume automatically; "
    "otherwise nothing will resume it, so tell the user to ask again once "
    "loading finishes, without naming a time."
)

_SQL_MEANWHILE = (
    "Meanwhile `list_tables`, `describe_table` and read-only `query` SQL work on the "
    "loaded tables if the user wants an answer now."
)

_SEMANTIC_REBUILDING_GUIDANCE = (
    "Data is loaded, and its data model (the semantic datasets) is being rebuilt "
    "automatically in the background. The rebuild reloads nothing and needs no "
    "approval. Do NOT call `run_materialization` for it and do NOT ask the user to "
    "approve a reload. Tell the user the data model is being rebuilt. Nothing will "
    "resume this conversation when it finishes, so tell them to ask again once it "
    "has, without naming a time, for answers that need `list_datasets` or "
    f"`semantic_query`. {_SQL_MEANWHILE} "
    "If the user asks for a data refresh meanwhile, `run_materialization` reports this "
    "rebuild as already running: tell them to ask again once it has finished."
)

_LOADED_NEEDS_RELOAD_GUIDANCE = (
    "Data is loaded, but no semantic datasets are available, and the data model "
    "cannot be rebuilt from it because the last load did not finish cleanly. Only a "
    "data refresh (`run_materialization`) fixes this; follow the refresh rules above "
    f"before starting one. {_SQL_MEANWHILE}"
)

_SEMANTIC_REBUILD_NOT_RUNNING_GUIDANCE = (
    "Data is loaded, but no semantic datasets are available and no automatic rebuild "
    "of the data model is running. Scout starts one itself, without reloading data, "
    "when a chat opens on loaded data it can build from, and does not retry a failed "
    "rebuild until the next load. "
    "Do NOT call `run_materialization` just to get the data model and do NOT ask the "
    "user to approve a reload for it; a reload fetches every source again and is for "
    "newer data the user asks for. Tell the user the data model could not be rebuilt "
    f"automatically. {_SQL_MEANWHILE}"
)

# Headless runs have no chat to start the rebuild and no user to ask, and the
# blocking load rebuilds the catalog before it returns.
_HEADLESS_LOADED_REBUILD_GUIDANCE = (
    "Data is loaded, but no semantic datasets are available yet. "
    "Run materialization to rebuild the semantic catalog, then use "
    "`list_datasets` and `semantic_query`."
)


# The chat a load is bound to (the one that started it, or the first chat in an
# unloaded workspace, #408) is resumed when it finishes.
_LOAD_STARTED_FOR_THIS_CHAT_GUIDANCE = (
    "This workspace's data is loading in the background; the load was started "
    "for this conversation. Do NOT call `run_materialization` or other data "
    "tools, and do NOT ask the user to start a load. Tell the user in one "
    "sentence that their data is loading and that you will continue with their "
    "request when it finishes, then end your turn. This conversation will "
    "resume automatically when loading completes."
)

# Any other chat gets no completion callback for someone else's load. Stating the
# fact rather than "do not promise a follow-up" matters: with only the prohibition,
# the agent still told prod users it would pick their question up.
_INTERACTIVE_MATERIALIZE_IN_PROGRESS_GUIDANCE = (
    "A data load is already in progress in the background, and it was not started "
    "for this conversation. Do NOT trigger another one and do NOT call other data "
    "tools. Nothing will resume this conversation when loading finishes: the user "
    "must ask again. Tell them plainly that their data is still loading and to ask "
    "their question again once it has finished, without naming a time. Then end "
    "your turn."
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

_READ_ONLY_LOADED_SQL_GUIDANCE = (
    "Data is loaded, but no semantic datasets are available yet. "
    "Use `list_tables` and `describe_table` to inspect the loaded tables, then "
    "read-only `query` SQL to analyze them. This user's workspace role is read-only; "
    "a read-write workspace role is required to rebuild the semantic catalog. If the "
    "user asks for semantic datasets or a refresh, a workspace member with write "
    "access can do it."
)

_READ_ONLY_MATERIALIZE_GUIDANCE = (
    "Data is not currently queryable, and this user's workspace role is read-only. "
    "A read-write workspace role is required to load data or rebuild the semantic catalog, "
    "so tell the user a workspace member with write access can refresh it. If no data "
    "has been loaded yet, the first chat such a member opens starts the load automatically."
)

_READ_ONLY_MATERIALIZE_IN_PROGRESS_GUIDANCE = (
    "A data load is already in progress for this workspace. This user's workspace "
    "role is read-only, so they cannot start or wait through another load. Report that "
    "the data is still loading. Nothing will resume this conversation when it "
    "finishes, so tell them to ask again once it has, without naming a time."
)


# Only shown when the semantic catalog is unavailable, so it covers raw SQL alone.
# The example name comes from the helper that mints the views (tests pin it).
_MULTI_TENANT_VIEW_NAME_EXAMPLE = view_name("<tenant_prefix>", "<table_name>")
_MULTI_TENANT_NAMESPACE_HINT = (
    "This is a multi-tenant workspace. In raw SQL, each tenant's tables are separate "
    f"views named `{_MULTI_TENANT_VIEW_NAME_EXAMPLE}`, where the prefix is derived from "
    "the tenant's name. Long prefixes and names are shortened with a hash, so never "
    "build a view name yourself: use the exact names `list_tables` returns. Each view "
    "holds only its own tenant's rows, so combine tenants with `UNION ALL` over their "
    "matching views, and JOIN only views that share a tenant prefix."
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


async def _build_system_prompt(
    workspace: Workspace,
    user,
    interactive: bool = True,
    canvas_write: bool = False,
    write_capable: bool = False,
    conversation_id: str | None = None,
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
    has_tenants = await workspace.tenants.aexists()
    remembers = interactive and has_personal_memory(user)
    personal_memory = await apersonal_memory_prompt(user) if remembers else ""
    stable = await _build_stable_system_prompt(
        workspace,
        user,
        has_tenants,
        interactive,
        canvas_write,
        write_capable,
        remembers=remembers,
        personal_memory=personal_memory,
    )
    volatile = ""
    if has_tenants:
        semantic_context = await _fetch_semantic_model_context(
            workspace, interactive, write_capable, conversation_id=conversation_id
        )
        volatile = f"\n## Data Availability\n\n{semantic_context}\n"
        if await workspace.tenants.acount() > 1:
            coverage = (
                await WorkspaceViewSchema.objects.filter(
                    workspace_id=workspace.id, state=SchemaState.ACTIVE
                )
                .values_list("tenant_coverage", flat=True)
                .afirst()
            )
            warning = coverage_warning(coverage)
            if warning:
                volatile += warning + "\n"
        freshness = _source_freshness_block(
            await aworkspace_source_freshness(workspace.id, getattr(user, "id", "")),
            write_capable=write_capable,
        )
        if freshness:
            volatile += f"\n{freshness}\n"
    return stable, volatile


def _fetch_age(fetched: datetime) -> str:
    # Calendar days in UTC, matching the date printed beside it.
    days = (timezone.now().astimezone(UTC).date() - fetched.astimezone(UTC).date()).days
    if days <= 0:
        return "today"
    return "1 day ago" if days == 1 else f"{days} days ago"


def _source_freshness_block(sources: list[dict], *, write_capable: bool) -> str:
    """Each source's data age and whether the latest load refreshed it (#715).

    ``last_materialized_at`` is the newest source's time, so one fresh source hid a
    month-old one and the agent blamed the loader instead of the expired sign-in.
    """
    if not any(s["serving"] or s["not_refreshed"] for s in sources):
        return ""
    lines = ["### Source Freshness", ""]
    for source in sources:
        fetched = parse_datetime(source["last_fetched_at"] or "")
        if fetched:
            age = f"data last fetched {fetched.astimezone(UTC):%Y-%m-%d %H:%M} UTC ({_fetch_age(fetched)})"
        elif source["serving"]:
            age = "data loaded, fetch time unknown"
        else:
            age = "no data fetched yet"
        line = f"- {source['name']} ({provider_label(source['provider'])}): {age}"
        if source["not_refreshed"]:
            why = (
                "the load stopped before this source"
                if source.get("stopped")
                else source.get("error_code") or "unknown error"
            )
            line += f". NOT refreshed by the latest load ({why})"
            if not source["serving"]:
                line += "; its data is not in what this workspace can query"
            line += f". Fix: {source['remedy']}."
        elif source["last_load"] == REUSED:
            line += "; the latest load reused this data without fetching it again."
        elif source["last_load"] == REFRESHED:
            line += "; refreshed by the latest load."
        lines.append(line)
    stale = [s for s in sources if s["not_refreshed"]]
    if stale:
        lines += [
            "",
            "When an answer depends on a source marked NOT refreshed, tell the user its "
            "data is only current to its fetch date, name the source, and give its fix.",
        ]
        if any(s.get("error_code") in CREDENTIAL_CODES for s in stale):
            lines.append(
                "For a sign-in problem, do not tell the user that a refresh cannot help or "
                "that the loader is at fault: the fix listed is what brings that source "
                "up to date."
            )
        if not write_capable:
            lines.append(
                "This user's workspace role is read-only, so a workspace member with "
                "write access has to refresh the data."
            )
    return "\n".join(lines)


async def _build_stable_system_prompt(
    workspace: Workspace,
    user,
    has_tenants: bool,
    interactive: bool,
    canvas_write: bool,
    write_capable: bool,
    *,
    remembers: bool = False,
    personal_memory: str = "",
) -> str:
    key = _system_prompt_cache_key(
        workspace, user, interactive, canvas_write, write_capable, personal_memory
    )
    cache_key = f"{key}:{has_tenants}"
    cached = _system_prompt_cache.get(cache_key)
    if cached is not None:
        value, timestamp = cached
        if time.monotonic() - timestamp < _SYSTEM_PROMPT_TTL:
            return value

    # Stable sections (cacheable prefix)
    stable_sections = [
        select_base_system_prompt(write_capable=write_capable, interactive=interactive),
        ARTIFACT_PROMPT_ADDITION if write_capable else ARTIFACT_READ_ONLY_PROMPT_ADDITION,
    ]

    if workspace.system_prompt:
        stable_sections.append(f"\n## Workspace Instructions\n\n{workspace.system_prompt}\n")

    if remembers:
        stable_sections.append(MEMORY_GUIDANCE)
        if personal_memory:
            stable_sections.append(personal_memory)

    retriever = KnowledgeRetriever(workspace)
    knowledge_context = await retriever.retrieve()
    if knowledge_context:
        # Retriever already emits a ``## Knowledge Base`` heading; don't double it
        # (arch #254, finding 01#4).
        stable_sections.append(f"\n{knowledge_context}\n")

    if has_tenants:
        stable_sections.append("""
## Workspace And Dataset Discovery

Semantic datasets are the primary objects users see and ask about. A dataset
has business-facing dimensions, time dimensions, measures, relationships,
labels, descriptions, and display metadata. Workspace names, providers,
pipelines, and dataset lists are runtime data; do not assume they are present
in the system prompt.

Use dataset tools by intent:
- Discover available data: `list_datasets` lists this workspace's datasets;
  `list_workspaces` lists the others. The query tools only read this workspace.
- Inspect one dataset's fields, labels, descriptions, formats, and
  relationships: `describe_dataset`.
- Answer analytical questions: `semantic_query` over semantic members.
- Reach data the semantic model does not express (raw text columns, columns with
  no semantic member): `list_tables` and `describe_table` to find them, then
  read-only `query` SQL.
- Change dataset definitions, labels, descriptions, fields, relationships, or
  display metadata: use the Semantic Canvas instructions below when available;
  do not mutate datasets directly from the parent chat agent.

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

    if interactive and canvas_write and write_capable:
        stable_sections.append("""
## Semantic Canvas (dataset editing)

This conversation has a canvas — a draft changeset over the workspace's
datasets, shown to the user in the side panel. When the user asks to edit a
dataset's label/description, field labels/descriptions, value format / display
format / number format or currency, add measures or dimensions, link datasets, or create a
CTE/SQL-derived dataset, delegate the whole job to the `canvas_manager` tool
with a complete task description (datasets, field names and source columns,
relationship endpoints, SQL, display metadata, and whether to commit). Use
`canvas_read` yourself to inspect pending draft changes, including after an
interrupted delegation. A Canvas Manager error does not roll back earlier
commits: use its committed_objects, cube_schema, and pending_count evidence,
then inspect/verify current state before a bounded retry. Do not recreate
already committed datasets or discard remaining drafts automatically.
Draft changes become queryable only after the canvas is committed.
Reusable CTE/SQL rules evaluate the current materialized source rows, including
newly materialized messages; they are not a fixed snapshot unless the SQL uses
an explicit snapshot or message-ID label mapping.
Autogenerated fields can be curated (label/description/format/currency) but
never removed or retyped.
For value format requests, tell `canvas_manager` to set field metadata `format`
and optional `currency`.

### Saving dataset changes
This user can change the data model, and every commit is saved as a revision
in the data model history that can be undone. So do NOT ask the user to approve
each change: when a question or artifact needs a custom dataset, dimension,
measure, relationship, or label/format edit, make the smallest change that
works and tell `canvas_manager` to commit it. State the assumptions you made
(grain, filters, groupings) alongside the result.
After every commit, tell the user exactly what changed, naming each dataset or
field, and how to undo it, e.g. "Created dataset visit_stats (undo it from Data
model history on the Datasets page)". To undo on request, have
`canvas_manager` undo that revision id.
Deletions are the exception. Deleting a dataset, or deleting or renaming a
field a saved artifact uses (including by undoing the revision that created
it), needs the user's explicit confirmation of that specific change first; the
commit or undo is refused until then. Ask, naming what will be deleted or renamed and the
artifacts that use it, and end your turn. Only when the user's next message
says yes, tell `canvas_manager` which objects they confirmed; a confirmation
is accepted only in a user turn after the question, within a few turns. A chart request
or a general "go ahead" is not confirmation of a deletion.
If `canvas_manager` reports fields whose definition changed while saved
artifacts use them, tell the user those artifacts may now show different numbers.
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
    _system_prompt_cache[cache_key] = (stable, time.monotonic())

    if len(_system_prompt_cache) > 256:
        now = time.monotonic()
        expired = [
            k for k, (_, ts) in _system_prompt_cache.items() if now - ts > _SYSTEM_PROMPT_TTL
        ]
        for k in expired:
            del _system_prompt_cache[k]

    return stable
