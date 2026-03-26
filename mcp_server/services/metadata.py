"""Pipeline-aware metadata service for MCP tools.

Provides enriched responses for list_tables, describe_table, and get_metadata
by combining MaterializationRun records with TenantMetadata discover-phase output
and pipeline registry definitions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apps.workspaces.models import MaterializationRun
from mcp_server.pipeline_registry import PipelineConfig
from mcp_server.services.query import _execute_sync_parameterized

if TYPE_CHECKING:
    from apps.workspaces.models import TenantMetadata, TenantSchema
    from mcp_server.context import QueryContext

logger = logging.getLogger(__name__)


def pipeline_list_tables(
    tenant_schema: TenantSchema,
    pipeline_config: PipelineConfig,
) -> list[dict]:
    """Return enriched table list from the last completed MaterializationRun.

    Returns an empty list if no completed run exists.
    Each entry includes name, type, description, row_count, and materialized_at.
    """
    run = (
        MaterializationRun.objects.filter(
            tenant_schema=tenant_schema,
            state=MaterializationRun.RunState.COMPLETED,
        )
        .order_by("-completed_at")
        .first()
    )
    if run is None:
        return []

    materialized_at = run.completed_at.isoformat() if run.completed_at else None
    sources_result: dict[str, Any] = (run.result or {}).get("sources", {})
    source_descriptions = {s.name: s.description for s in pipeline_config.sources}
    source_physical_names = {s.name: s.physical_table_name for s in pipeline_config.sources}

    tables = []
    for source_name, source_data in sources_result.items():
        tables.append(
            {
                "name": source_physical_names.get(source_name, f"raw_{source_name}"),
                "type": "table",
                "description": source_descriptions.get(source_name, ""),
                "row_count": source_data.get("rows"),
                "materialized_at": materialized_at,
            }
        )

    for model_name in pipeline_config.dbt_models:
        tables.append(
            {
                "name": model_name,
                "type": "table",
                "description": "",
                "row_count": None,
                "materialized_at": materialized_at,
            }
        )

    return tables


def workspace_list_tables(ctx: QueryContext) -> list[dict]:
    """Return table list for a workspace view schema by querying information_schema directly.

    Used when the schema is a WorkspaceViewSchema (namespaced views) rather than a
    TenantSchema backed by a MaterializationRun. Returns one entry per view found.
    """
    result = _execute_sync_parameterized(
        ctx,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'VIEW' "
        "ORDER BY table_name",
        (ctx.schema_name,),
        ctx.max_query_timeout_seconds,
    )
    return [
        {
            "name": row[0],
            "type": "view",
            "description": "",
            "row_count": None,
            "materialized_at": None,
        }
        for row in (result.get("rows") or [])
    ]


def pipeline_describe_table(
    table_name: str,
    ctx: QueryContext,
    tenant_metadata: TenantMetadata | None,
    pipeline_config: PipelineConfig,
) -> dict | None:
    """Describe a table using information_schema, enriched with discover-phase annotations.

    Returns None if the table does not exist in information_schema.
    JSONB columns (properties, form_data) receive descriptions derived from TenantMetadata.
    """
    result = _execute_sync_parameterized(
        ctx,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (ctx.schema_name, table_name),
        ctx.max_query_timeout_seconds,
    )

    if not result.get("rows"):
        return None

    source_descriptions = {s.physical_table_name: s.description for s in pipeline_config.sources}
    jsonb_annotations = _build_jsonb_annotations(table_name, tenant_metadata)

    columns = []
    for row in result["rows"]:
        col_name, data_type, is_nullable, default = row
        columns.append(
            {
                "name": col_name,
                "type": data_type,
                "nullable": is_nullable == "YES",
                "default": default,
                "description": jsonb_annotations.get(col_name, ""),
            }
        )

    return {
        "name": table_name,
        "description": source_descriptions.get(table_name, ""),
        "columns": columns,
    }


def _build_jsonb_annotations(
    table_name: str, tenant_metadata: TenantMetadata | None
) -> dict[str, str]:
    """Build per-column description strings for known JSONB columns.

    Returns an empty dict if TenantMetadata is absent or the table has no annotations.
    """
    if tenant_metadata is None:
        return {}

    metadata = tenant_metadata.metadata or {}

    if table_name == "raw_cases":
        case_types = metadata.get("case_types", [])
        if case_types:
            names = ", ".join(ct["name"] for ct in case_types)
            return {"properties": f"Contains case properties. Available case types: {names}"}

    elif table_name == "raw_forms":
        form_definitions = metadata.get("form_definitions", {})
        if form_definitions:
            names = []
            for xmlns, fd in form_definitions.items():
                name = fd.get("name", xmlns)
                if isinstance(name, dict):
                    # name is a translations dict e.g. {"en": "My Form"} — take first value
                    name = next(iter(name.values()), xmlns)
                names.append(str(name))
            form_names = ", ".join(names)
            return {"form_data": f"Contains form submission data. Available forms: {form_names}"}

    return {}


def transformation_aware_list_tables(
    tenant_schema: TenantSchema,
    pipeline_config: PipelineConfig,
    tenant_ids: list,
    workspace_id=None,
) -> list[dict]:
    """List tables combining raw sources with terminal transformation assets.

    If TransformationAsset records exist for the tenant, terminal models
    replace their upstream tables in the listing. Otherwise falls back
    to the standard pipeline_list_tables behavior.
    """
    from apps.transformations.services.lineage import get_terminal_assets

    terminal_assets = get_terminal_assets(tenant_ids, workspace_id)

    if not terminal_assets:
        # No transformation assets — use existing pipeline-based listing
        return pipeline_list_tables(tenant_schema, pipeline_config)

    # Build set of replaced table names (walk replaces chains)
    replaced_names = set()
    for asset in terminal_assets:
        current = asset.replaces
        visited = set()
        while current and current.id not in visited:
            visited.add(current.id)
            replaced_names.add(current.name)
            current = current.replaces

    # Start with raw tables, excluding replaced ones and terminal asset names
    raw_tables = pipeline_list_tables(tenant_schema, pipeline_config)
    terminal_names = {asset.name for asset in terminal_assets}
    result = [
        t for t in raw_tables if t["name"] not in replaced_names and t["name"] not in terminal_names
    ]

    # Add terminal transformation assets
    for asset in terminal_assets:
        result.append(
            {
                "name": asset.name,
                "type": "table",
                "description": asset.description,
                "row_count": None,
                "materialized_at": None,
            }
        )

    return result


def pipeline_get_metadata(
    tenant_schema: TenantSchema,
    ctx: QueryContext,
    tenant_metadata: TenantMetadata | None,
    pipeline_config: PipelineConfig,
) -> dict:
    """Return full metadata snapshot: tables with enriched columns and pipeline relationships.

    Returns {"tables": {}, "relationships": []} if no completed run exists.
    """
    tables_list = pipeline_list_tables(tenant_schema, pipeline_config)
    if not tables_list:
        return {"tables": {}, "relationships": []}

    tables = {}
    for t in tables_list:
        detail = pipeline_describe_table(t["name"], ctx, tenant_metadata, pipeline_config)
        if detail:
            tables[t["name"]] = detail

    relationships = [
        {
            "from_table": r.from_table,
            "from_column": r.from_column,
            "to_table": r.to_table,
            "to_column": r.to_column,
            "description": r.description,
        }
        for r in pipeline_config.relationships
    ]

    return {"tables": tables, "relationships": relationships}
