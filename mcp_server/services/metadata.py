"""Pipeline-aware metadata service for MCP tools.

Provides enriched responses for list_tables, describe_table, and get_metadata
by combining MaterializationRun records with TenantMetadata discover-phase output
and pipeline registry definitions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apps.projects.models import MaterializationRun
from mcp_server.pipeline_registry import PipelineConfig
from mcp_server.services.query import _execute_sync_parameterized

if TYPE_CHECKING:
    from apps.projects.models import TenantMetadata, TenantSchema
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

    tables = []
    for source_name, source_data in sources_result.items():
        tables.append(
            {
                "name": source_name,
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

    source_descriptions = {s.name: s.description for s in pipeline_config.sources}
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

    if table_name == "cases":
        case_types = metadata.get("case_types", [])
        if case_types:
            names = ", ".join(ct["name"] for ct in case_types)
            return {"properties": f"Contains case properties. Available case types: {names}"}

    elif table_name == "forms":
        form_definitions = metadata.get("form_definitions", {})
        if form_definitions:
            form_names = ", ".join(fd.get("name", xmlns) for xmlns, fd in form_definitions.items())
            return {"form_data": f"Contains form submission data. Available forms: {form_names}"}

    return {}


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
