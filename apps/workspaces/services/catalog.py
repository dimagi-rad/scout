"""One reconciled catalog + describe read-model (arch #251, Phase 3).

Single source of truth for "what tables exist" across every surface: the agent
prompt (``apps.agents.graph.base``), the MCP ``list_tables`` /
``describe_table`` / ``get_metadata`` tools, and the DRF data dictionary. This
replaces five divergent listers (the #190 panic-loop class) with ONE service
that applies:

- **Uniform, fail-closed physical reconciliation** — every entry (raw source,
  terminal transformation asset, view) appears ONLY if its physical table/view is
  present in the live schema. So ``list_catalog`` can never name a table that
  ``describe``/``query`` then 404s, and a terminal asset whose table is absent is
  silently dropped rather than advertised.
- **ONE ``stg_*`` policy (Decision 4a)** — intermediate ``stg_*`` staging models
  are hidden on every surface; terminal assets are surfaced when physically
  present.

Read-only: this observes MaterializationRun / TransformationAsset / the live
``information_schema`` but never writes schema/run state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from django.db import models

from apps.transformations.models import TransformationAsset
from apps.transformations.services.lineage import aget_terminal_assets, get_terminal_assets
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceViewSchema,
)
from apps.workspaces.services.pipeline_resolver import aresolve_pipeline_config
from mcp_server.context import QueryContext, load_workspace_context
from mcp_server.pipeline_registry import PipelineConfig, get_registry
from mcp_server.services.metadata import (
    _live_tables_in_schema,
    pipeline_describe_table,
    workspace_list_tables,
)

logger = logging.getLogger(__name__)

_COMPLETED_OR_PARTIAL = (
    MaterializationRun.RunState.COMPLETED,
    MaterializationRun.RunState.PARTIAL,
)


@dataclass
class CatalogTable:
    """One reconciled catalog entry. ``verified`` is always True here: an entry
    only exists after passing the live-schema reconciliation."""

    name: str
    type: str  # "source" | "transform" | "view"
    logical_name: str | None
    description: str
    row_count: int | None
    materialized_at: datetime | None
    verified: bool


@dataclass
class TableDescription:
    """Column-level detail for one table — the ONE describe path."""

    name: str
    description: str
    columns: list[dict]

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "columns": self.columns}


@dataclass(frozen=True)
class CatalogContext:
    """Resolved routing context for a workspace's catalog.

    ``query_context`` is the ``QueryContext`` from ``mcp_server.context`` (used to
    read live ``information_schema`` for view schemas and for ``describe``). For a
    single-tenant list it may be ``None`` — the source reconciliation reads live
    tables via the schema name alone.

    - Single-tenant: ``is_view_schema=False``, ``tenant_schema`` set, source of
      truth is the last COMPLETED/PARTIAL ``MaterializationRun`` plus terminal
      ``TransformationAsset``s.
    - Multi-tenant: ``is_view_schema=True``, ``tenant_schema`` None, source of
      truth is the ``ws_*`` view schema's ``information_schema`` VIEWs.
    """

    query_context: QueryContext | None
    is_view_schema: bool
    tenant_schema: TenantSchema | None
    pipeline_config: PipelineConfig | None
    tenant_ids: list = field(default_factory=list)
    workspace_id: str | None = None


def _is_staging(name: str) -> bool:
    """True for an intermediate staging model (hidden everywhere, Decision 4a).

    Handles namespaced multi-tenant view names (``tenant__stg_foo``) by testing
    the logical portion after the final ``__``.
    """
    return name.rsplit("__", 1)[-1].startswith("stg_")


def to_tool_dict(t: CatalogTable) -> dict:
    """Serialize a CatalogTable to the MCP/prompt table dict shape.

    Keeps the historical keys (``type`` collapses source/transform to ``table``;
    ``materialized_row_count`` paired with ``row_count_verified: False`` because
    it is the count recorded at materialization time, not a live count).
    """
    materialized_at = t.materialized_at
    if isinstance(materialized_at, datetime):
        materialized_at = materialized_at.isoformat()
    return {
        "name": t.name,
        "type": "view" if t.type == "view" else "table",
        "description": t.description,
        "materialized_row_count": t.row_count,
        "row_count_verified": False,
        "materialized_at": materialized_at,
    }


def _source_maps(pipeline_config: PipelineConfig | None) -> tuple[dict, dict]:
    if pipeline_config is None:
        return {}, {}
    descriptions = {s.name: s.description for s in pipeline_config.sources}
    physical = {s.name: s.physical_table_name for s in pipeline_config.sources}
    return descriptions, physical


def _reconcile_run_entries(run, pipeline_config, live: set[str]) -> list[CatalogTable]:
    """Fail-closed source entries from a MaterializationRun record.

    Only ``completed`` sources whose physical table is live are surfaced (excludes
    failed/skipped/in_progress sources, issue #187, and torn-down tables, #185).

    The pipeline dbt-model listing path was removed in Phase 5 (#251): no shipped
    pipeline declares ``transforms`` so ``dbt_models`` was always empty, and the
    loop only ever produced phantom entries. Workspace-scope transforms surface as
    terminal ``TransformationAsset``s via ``_merge_terminal_entries`` instead.
    """
    if run is None:
        return []
    materialized_at = run.completed_at
    sources = (run.result or {}).get("sources", {})
    descriptions, physical = _source_maps(pipeline_config)

    entries: list[CatalogTable] = []
    for source_name, source_data in sources.items():
        if (source_data or {}).get("state") != "completed":
            continue
        name = physical.get(source_name, f"raw_{source_name}")
        if name not in live:
            continue
        entries.append(
            CatalogTable(
                name=name,
                type="source",
                logical_name=name,
                description=descriptions.get(source_name, ""),
                row_count=(source_data or {}).get("rows"),
                materialized_at=materialized_at,
                verified=True,
            )
        )

    return entries


def _terminal_asset_entry(asset) -> CatalogTable:
    return CatalogTable(
        name=asset.name,
        type="transform",
        logical_name=asset.name,
        description=asset.description,
        row_count=None,
        materialized_at=None,
        verified=True,
    )


def _visible_q(tenant_ids: list, workspace_id) -> models.Q:
    q = models.Q(tenant_id__in=tenant_ids)
    if workspace_id:
        q = q | models.Q(workspace_id=workspace_id)
    return q


async def _areplaced_names(terminal_assets, tenant_ids: list, workspace_id) -> set[str]:
    """Names of upstream assets replaced by a terminal one (async).

    Walks the ``replaces`` chain scoped to visible assets only, so a terminal
    model hides the tables it supersedes without cross-tenant disclosure.
    """
    visible_q = _visible_q(tenant_ids, workspace_id)
    replaced: set[str] = set()
    for asset in terminal_assets:
        next_id = asset.replaces_id
        visited: set = set()
        while next_id and next_id not in visited:
            visited.add(next_id)
            upstream = await TransformationAsset.objects.filter(visible_q, id=next_id).afirst()
            if upstream is None:
                break
            replaced.add(upstream.name)
            next_id = upstream.replaces_id
    return replaced


def _replaced_names_sync(terminal_assets, tenant_ids: list, workspace_id) -> set[str]:
    """Sync sibling of ``_areplaced_names`` for the DRF data dictionary."""
    visible_q = _visible_q(tenant_ids, workspace_id)
    replaced: set[str] = set()
    for asset in terminal_assets:
        next_id = asset.replaces_id
        visited: set = set()
        while next_id and next_id not in visited:
            visited.add(next_id)
            upstream = TransformationAsset.objects.filter(visible_q, id=next_id).first()
            if upstream is None:
                break
            replaced.add(upstream.name)
            next_id = upstream.replaces_id
    return replaced


def _merge_terminal_entries(
    entries: list[CatalogTable],
    terminal_assets,
    replaced_names: set[str],
    live: set[str],
) -> list[CatalogTable]:
    """Fold terminal transformation assets into ``entries``, fail-closed.

    A terminal asset replaces its upstream tables (dropped from ``entries``) and
    is itself surfaced ONLY if its physical table is live — this is the #190 fix:
    the prompt can no longer name a terminal asset the ``list_tables`` tool omits.
    """
    if not terminal_assets:
        return entries
    terminal_names = {a.name for a in terminal_assets}
    kept = [e for e in entries if e.name not in replaced_names and e.name not in terminal_names]
    for asset in terminal_assets:
        if asset.name in live:
            kept.append(_terminal_asset_entry(asset))
    return kept


async def _tenant_ids_for(context: CatalogContext) -> list:
    if context.tenant_ids:
        return context.tenant_ids
    if context.tenant_schema is not None:
        return [context.tenant_schema.tenant_id]
    return []


async def _list_single_tenant(context: CatalogContext) -> list[CatalogTable]:
    ts = context.tenant_schema
    if ts is None:
        return []
    run = (
        await MaterializationRun.objects.filter(tenant_schema=ts, state__in=_COMPLETED_OR_PARTIAL)
        .order_by("-completed_at")
        .afirst()
    )
    live = await _live_tables_in_schema(ts.schema_name)
    entries = _reconcile_run_entries(run, context.pipeline_config, live)

    tenant_ids = await _tenant_ids_for(context)
    terminal = await aget_terminal_assets(tenant_ids, context.workspace_id)
    replaced = await _areplaced_names(terminal, tenant_ids, context.workspace_id)
    return _merge_terminal_entries(entries, terminal, replaced, live)


async def _list_view_schema(context: CatalogContext) -> list[CatalogTable]:
    if context.query_context is None:
        return []
    rows = await workspace_list_tables(context.query_context)
    return [
        CatalogTable(
            name=row["name"],
            type="view",
            logical_name=row["name"],
            description=row.get("description", ""),
            row_count=None,
            materialized_at=None,
            verified=True,
        )
        for row in rows
    ]


async def list_catalog(context: CatalogContext) -> list[CatalogTable]:
    """The ONE reconciled catalog lister. Replaces the five divergent listers.

    Applies uniform fail-closed reconciliation and the single ``stg_*`` policy
    for both single-tenant (MaterializationRun + terminal assets) and multi-tenant
    (``ws_*`` view schema) workspaces.
    """
    if context.is_view_schema:
        entries = await _list_view_schema(context)
    else:
        entries = await _list_single_tenant(context)
    return [e for e in entries if not _is_staging(e.name)]


def list_catalog_sync(
    tenant_schema,
    pipeline_config,
    live_table_names: set[str],
    *,
    workspace_id=None,
) -> list[dict]:
    """Sync sibling of ``list_catalog`` for the single-tenant DRF data dictionary.

    Returns the same reconciled table set (same source-of-truth, same fail-closed
    reconciliation, same ``stg_*`` policy, including terminal assets) as the async
    ``list_catalog``, so the dictionary agrees with the prompt and the MCP tools.
    Kept sync (no ``async_to_sync``) so the data-dictionary view stays
    single-connection and event-loop-free (arch #254, finding 10#2); the caller
    supplies ``live_table_names`` read from its one managed-DB connection.
    """
    run = (
        MaterializationRun.objects.filter(
            tenant_schema=tenant_schema, state__in=_COMPLETED_OR_PARTIAL
        )
        .order_by("-completed_at")
        .first()
    )
    entries = _reconcile_run_entries(run, pipeline_config, live_table_names)

    tenant_ids = [tenant_schema.tenant_id]
    terminal = get_terminal_assets(tenant_ids, workspace_id)
    replaced = _replaced_names_sync(terminal, tenant_ids, workspace_id)
    entries = _merge_terminal_entries(entries, terminal, replaced, live_table_names)
    return [to_tool_dict(e) for e in entries if not _is_staging(e.name)]


async def describe(
    context: CatalogContext, table: str, tenant_metadata=None
) -> TableDescription | None:
    """The ONE column/describe path. Reads columns from the live
    ``information_schema`` for the context's schema (works uniformly for a
    single-tenant ``t_*`` schema and a multi-tenant ``ws_*`` view schema — this is
    the ``get_metadata`` ws_* lookup fix). Returns None if the table is absent.

    ``tenant_metadata`` is supplied by the caller from the ONE deterministic,
    live-filtered read (arch #251, Phase 4, Decision 5 — ``tenant_metadata``
    service), so every surface annotates columns identically.
    """
    if context.query_context is None:
        return None
    # pipeline_config is None only for a multi-tenant ws_* view schema, where no
    # single tenant pipeline exists; commcare_sync is a describe-time placeholder
    # for source descriptions that never match namespaced view names (so it can't
    # surface wrong-provider text). Single-tenant callers always pass a resolved
    # config or return a truthful error before reaching here.
    pipeline_config = context.pipeline_config or get_registry().get("commcare_sync")
    detail = await pipeline_describe_table(
        table, context.query_context, tenant_metadata, pipeline_config
    )
    if detail is None:
        return None
    return TableDescription(
        name=detail["name"], description=detail["description"], columns=detail["columns"]
    )


async def catalog_metadata(context: CatalogContext, tenant_metadata=None) -> dict:
    """Full metadata snapshot: every reconciled table with columns, plus pipeline
    relationships. Built from ``list_catalog`` + ``describe`` so it can never name
    a table the catalog omits, and returns real columns for multi-tenant view
    schemas (the ``get_metadata`` table_count=0 fix)."""
    tables_list = await list_catalog(context)
    tables: dict = {}
    for t in tables_list:
        detail = await describe(context, t.name, tenant_metadata)
        if detail is not None:
            tables[t.name] = detail.as_dict()

    relationships = []
    cfg = context.pipeline_config
    if cfg is not None:
        relationships = [
            {
                "from_table": r.from_table,
                "from_column": r.from_column,
                "to_table": r.to_table,
                "to_column": r.to_column,
                "description": r.description,
            }
            for r in cfg.relationships
        ]
    return {"tables": tables, "relationships": relationships}


async def resolve_catalog_context(workspace_id: str) -> CatalogContext:
    """Resolve a CatalogContext from a workspace id (raises ValueError if the
    workspace has no active routing target — same contract as
    ``load_workspace_context``).

    Single-tenant workspaces resolve to a ``t_*`` TenantSchema; multi-tenant to a
    ``ws_*`` view schema. Callers that already resolved these pieces (the MCP
    tools) build ``CatalogContext`` directly instead.
    """
    query_context = await load_workspace_context(workspace_id)
    is_view = await WorkspaceViewSchema.objects.filter(
        schema_name=query_context.schema_name, state=SchemaState.ACTIVE
    ).aexists()

    tenant_schema = None
    pipeline_config = None
    tenant_ids: list = []
    if is_view:
        workspace = await Workspace.objects.aget(id=workspace_id)
        tenant_ids = [tid async for tid in workspace.tenants.values_list("id", flat=True)]
    else:
        tenant_schema = await TenantSchema.objects.filter(
            schema_name=query_context.schema_name
        ).afirst()
        last_run = None
        if tenant_schema is not None:
            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=tenant_schema, state__in=_COMPLETED_OR_PARTIAL
                )
                .order_by("-completed_at")
                .afirst()
            )
            tenant_ids = [tenant_schema.tenant_id]
        pipeline_config = await aresolve_pipeline_config(tenant_schema, last_run)

    return CatalogContext(
        query_context=query_context,
        is_view_schema=is_view,
        tenant_schema=tenant_schema,
        pipeline_config=pipeline_config,
        tenant_ids=tenant_ids,
        workspace_id=str(workspace_id),
    )
