"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

Tools receive a workspace_id (injected server-side by the agent graph)
to route queries to the correct schema. All responses use a consistent
envelope format.

Usage:
    # stdio transport (for local clients)
    python -m mcp_server

    # HTTP transport (for networked clients)
    python -m mcp_server --transport streamable-http
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError as _ValidationError
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from apps.users.services.credential_resolver import aresolve_credential
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantMetadata,
    TenantSchema,
    WorkspaceViewSchema,
)
from mcp_server.context import load_workspace_context
from mcp_server.envelope import (
    AUTH_TOKEN_EXPIRED,
    INTERNAL_ERROR,
    NOT_FOUND,
    VALIDATION_ERROR,
    error_response,
    success_response,
    tool_context,
)
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import run_pipeline
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_get_metadata,
    pipeline_list_tables,
    workspace_list_tables,
)
from mcp_server.services.query import execute_query

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


async def _resolve_mcp_context(workspace_id: str):
    """Load a QueryContext for the workspace."""
    if not workspace_id:
        raise ValueError("workspace_id is required")
    return await load_workspace_context(workspace_id)


# --- Tools ---


@mcp.tool()
async def list_tables(workspace_id: str = "") -> dict:
    """List all tables in the workspace's database schema.

    Returns table names, types, descriptions, row counts, and materialization timestamps.
    Returns an empty list if no materialization run has completed yet.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("list_tables", workspace_id) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        # For multi-tenant workspaces, the context points at a WorkspaceViewSchema
        # (namespaced views). Use information_schema directly instead of MaterializationRun.
        if workspace_id:
            is_view_schema = await WorkspaceViewSchema.objects.filter(
                schema_name=ctx.schema_name, state=SchemaState.ACTIVE
            ).aexists()
            if is_view_schema:
                tables = await sync_to_async(workspace_list_tables)(ctx)
                tc["result"] = success_response(
                    {"tables": tables, "note": None},
                    schema=ctx.schema_name,
                    timing_ms=tc["timer"].elapsed_ms,
                )
                return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"tables": [], "note": None},
                schema=ctx.schema_name,
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema=ts,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        tables = await sync_to_async(pipeline_list_tables)(ts, pipeline_config)

        note = (
            "No completed materialization run found. Run run_materialization to load data."
            if not tables
            else None
        )
        tc["result"] = success_response(
            {"tables": tables, "note": note},
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def describe_table(table_name: str, workspace_id: str = "") -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default, description) and a table description.
    JSONB columns are annotated with summaries from the CommCare discover phase when available.

    Args:
        table_name: Name of the table to describe.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("describe_table", workspace_id, table_name=table_name) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()

        last_run = None
        tenant_metadata = None
        if ts is not None:
            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=ts,
                    state=MaterializationRun.RunState.COMPLETED,
                )
                .order_by("-completed_at")
                .afirst()
            )
            tenant_metadata = await TenantMetadata.objects.filter(
                tenant_membership__tenant_id=ts.tenant_id
            ).afirst()

        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        table = await sync_to_async(pipeline_describe_table)(
            table_name, ctx, tenant_metadata, pipeline_config
        )
        if table is None:
            tc["result"] = error_response(
                NOT_FOUND, f"Table '{table_name}' not found in schema '{ctx.schema_name}'"
            )
            return tc["result"]

        tc["result"] = success_response(
            table,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_metadata(workspace_id: str = "") -> dict:
    """Get a complete metadata snapshot for the workspace's database.

    Returns all tables with their columns, descriptions, and table relationships
    defined by the materialization pipeline.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("get_metadata", workspace_id) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"schema": ctx.schema_name, "table_count": 0, "tables": {}, "relationships": []},
                schema=ctx.schema_name,
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema=ts,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        tenant_metadata = await TenantMetadata.objects.filter(
            tenant_membership__tenant_id=ts.tenant_id
        ).afirst()

        metadata = await sync_to_async(pipeline_get_metadata)(
            ts, ctx, tenant_metadata, pipeline_config
        )

        tc["result"] = success_response(
            {
                "schema": ctx.schema_name,
                "table_count": len(metadata["tables"]),
                "tables": metadata["tables"],
                "relationships": metadata["relationships"],
            },
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_lineage(model_name: str, workspace_id: str = "") -> dict:
    """Get the transformation lineage for a model.

    Returns the chain of transformations from the given model back to the raw
    source data, showing what each step does and why. Use this when the user
    asks about data provenance, how a table was created, or what cleaning
    or transformations were applied to the data.

    Args:
        model_name: Name of the model to trace lineage for.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    from apps.transformations.services.lineage import get_lineage_chain
    from apps.workspaces.models import Workspace

    async with tool_context("get_lineage", workspace_id, model_name=model_name) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        try:
            workspace = await Workspace.objects.aget(id=workspace_id)
        except Workspace.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Workspace '{workspace_id}' not found")
            return tc["result"]

        tenant_ids = [t.id async for t in workspace.tenants.all()]

        chain = await sync_to_async(get_lineage_chain)(
            model_name, tenant_ids=tenant_ids, workspace_id=workspace_id
        )

        if not chain:
            tc["result"] = error_response(
                NOT_FOUND, f"No transformation asset named '{model_name}' found"
            )
            return tc["result"]

        tc["result"] = success_response(
            {"model": model_name, "lineage": chain},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def query(sql: str, workspace_id: str = "") -> dict:
    """Execute a read-only SQL query against the workspace's database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        sql: A SQL SELECT query to execute.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("query", workspace_id, sql=sql) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        result = await execute_query(ctx, sql)

        # execute_query returns an error envelope on failure
        if not result.get("success", True):
            tc["result"] = result
            return tc["result"]

        warnings = []
        if result.get("truncated"):
            warnings.append(f"Results truncated to {ctx.max_rows_per_query} rows")

        tc["result"] = success_response(
            {
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result.get("truncated", False),
                "sql_executed": result.get("sql_executed", ""),
                "tables_accessed": result.get("tables_accessed", []),
            },
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
            warnings=warnings or None,
        )
        return tc["result"]


@mcp.tool()
async def list_pipelines() -> dict:
    """List available materialization pipelines and their descriptions.

    Returns the registry of pipelines that can be run via run_materialization.
    Each entry includes the pipeline name, description, provider, sources, and DBT models.
    """
    async with tool_context("list_pipelines", "") as tc:
        registry = get_registry()
        pipelines = [
            {
                "name": p.name,
                "description": p.description,
                "provider": p.provider,
                "version": p.version,
                "sources": [{"name": s.name, "description": s.description} for s in p.sources],
                "has_metadata_discovery": p.has_metadata_discovery,
                "dbt_models": p.dbt_models,
            }
            for p in registry.list()
        ]
        tc["result"] = success_response(
            {"pipelines": pipelines},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_materialization_status(run_id: str) -> dict:
    """Retrieve the status of a materialization run by ID.

    Primarily a fallback for reconnection scenarios — live progress is delivered
    via MCP progress notifications during an active run_materialization call.

    Args:
        run_id: UUID of the MaterializationRun to look up.
    """
    async with tool_context("get_materialization_status", run_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related("tenant_schema__tenant").aget(
                id=run_id
            )
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        tenant_id = run.tenant_schema.tenant.external_id
        schema = run.tenant_schema.schema_name

        tc["result"] = success_response(
            {
                "run_id": str(run.id),
                "pipeline": run.pipeline,
                "state": run.state,
                "result": run.result,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "tenant_id": tenant_id,
            },
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def cancel_materialization(run_id: str) -> dict:
    """Cancel a running materialization pipeline.

    Marks the run as failed in the database. This is a best-effort cancellation —
    in-flight loader operations may not terminate immediately. Full subprocess
    cancellation is a future feature.

    Args:
        run_id: UUID of the MaterializationRun to cancel.
    """
    async with tool_context("cancel_materialization", run_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related("tenant_schema__tenant").aget(
                id=run_id
            )
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        in_progress = {
            MaterializationRun.RunState.STARTED,
            MaterializationRun.RunState.DISCOVERING,
            MaterializationRun.RunState.LOADING,
            MaterializationRun.RunState.TRANSFORMING,
        }
        if run.state not in in_progress:
            tc["result"] = error_response(
                VALIDATION_ERROR,
                f"Run '{run_id}' is not in progress (state: {run.state})",
            )
            return tc["result"]

        previous_state = run.state
        run.state = MaterializationRun.RunState.FAILED
        run.completed_at = datetime.now(UTC)
        run.result = {**(run.result or {}), "cancelled": True}
        await sync_to_async(run.save)(update_fields=["state", "completed_at", "result"])

        tenant_id = run.tenant_schema.tenant.external_id
        schema = run.tenant_schema.schema_name
        logger.info("Cancelled run %s for tenant %s (was: %s)", run_id, tenant_id, previous_state)

        tc["result"] = success_response(
            {"run_id": run_id, "cancelled": True, "previous_state": previous_state},
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


async def _materialize_tenant(
    tm,
    pipeline_config,
    ctx: Context | None,
) -> dict:
    """Run a materialization pipeline for a single TenantMembership.

    Resolves credentials, builds a progress callback, and executes the pipeline.
    Returns the pipeline result dict on success, or raises on failure.
    """
    from mcp_server.loaders.commcare_base import CommCareAuthError
    from mcp_server.loaders.connect_base import ConnectAuthError

    tenant_id = tm.tenant.external_id

    # ── Resolve credential ────────────────────────────────────────────────
    credential = await aresolve_credential(tm)
    if credential is None:
        return error_response("AUTH_TOKEN_MISSING", "No credential configured for this tenant")

    # ── Build progress callback ───────────────────────────────────────────
    progress_callback = None
    if ctx is not None:
        loop = asyncio.get_running_loop()

        def _on_progress_done(fut):
            exc = fut.exception()
            if exc is not None:
                logger.warning("Progress notification delivery failed: %s", exc)

        def progress_callback(current: int, total: int, message: str) -> None:
            fut = asyncio.run_coroutine_threadsafe(
                ctx.report_progress(current, total, message),
                loop,
            )
            fut.add_done_callback(_on_progress_done)

    # ── Run pipeline ──────────────────────────────────────────────────────
    try:
        return await sync_to_async(run_pipeline)(tm, credential, pipeline_config, progress_callback)
    except (CommCareAuthError, ConnectAuthError) as e:
        logger.warning("Auth failed for tenant %s: %s", tenant_id, e)
        return error_response(AUTH_TOKEN_EXPIRED, str(e))
    except Exception:
        logger.exception("Pipeline '%s' failed for tenant %s", pipeline_config.name, tenant_id)
        return error_response(INTERNAL_ERROR, f"Pipeline '{pipeline_config.name}' failed")


async def _resolve_workspace_memberships(workspace_id, user_id):
    """Resolve TenantMemberships for all tenants in a workspace."""
    from apps.users.models import TenantMembership
    from apps.workspaces.models import Workspace, WorkspaceTenant

    workspace = await Workspace.objects.filter(id=workspace_id).afirst()
    if workspace is None:
        return None, f"Workspace '{workspace_id}' not found"

    tenant_ids = [
        wt.tenant_id
        async for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant")
    ]
    if not tenant_ids:
        return None, "Workspace has no tenants configured"

    qs = TenantMembership.objects.select_related("user", "tenant").filter(tenant_id__in=tenant_ids)
    if user_id:
        qs = qs.filter(user_id=user_id)

    memberships = [tm async for tm in qs]
    if not memberships:
        return None, "No tenant memberships found for this user in this workspace"

    return memberships, None


@mcp.tool()
async def run_materialization(
    workspace_id: str = "",
    user_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Materialize data for all tenants in the workspace.

    Resolves all tenants linked to the workspace and runs the appropriate
    materialization pipeline for each one. Creates schemas automatically
    if they don't exist. Streams progress via MCP notifications/progress
    when the caller provides a progressToken.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: User UUID (injected server-side by the agent graph).
    """
    async with tool_context("run_materialization", workspace_id) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        memberships, err = await _resolve_workspace_memberships(workspace_id, user_id)
        if err:
            tc["result"] = error_response(NOT_FOUND, err)
            return tc["result"]

        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}

        results = []
        for tm in memberships:
            pipeline_name = provider_pipeline_map.get(tm.tenant.provider)
            if pipeline_name is None:
                results.append(
                    {
                        "tenant": tm.tenant.external_id,
                        "success": False,
                        "error": f"No pipeline for provider '{tm.tenant.provider}'",
                    }
                )
                continue

            pipeline_config = registry.get(pipeline_name)
            result = await _materialize_tenant(tm, pipeline_config, ctx)

            # _materialize_tenant returns an error envelope dict or a pipeline result dict
            if isinstance(result, dict) and not result.get("success", True):
                results.append(
                    {
                        "tenant": tm.tenant.external_id,
                        "success": False,
                        "error": result.get("error", {}).get("message", "Unknown error"),
                    }
                )
            else:
                results.append(
                    {
                        "tenant": tm.tenant.external_id,
                        "success": True,
                        "result": result,
                    }
                )

        all_succeeded = all(r["success"] for r in results)
        tc["result"] = success_response(
            {"tenants": results, "all_succeeded": all_succeeded},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_schema_status(workspace_id: str = "") -> dict:
    """Check whether data has been loaded for this workspace.

    Returns schema existence, state, last materialization timestamp, and table
    list. Always succeeds — returns exists=False if no schema has been
    provisioned yet. Safe to call before any data has been loaded.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    from apps.workspaces.models import (
        MaterializationRun,
        SchemaState,
        TenantSchema,
        Workspace,
        WorkspaceViewSchema,
    )

    async with tool_context("get_schema_status", workspace_id) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        not_provisioned = success_response(
            {
                "exists": False,
                "state": "not_provisioned",
                "last_materialized_at": None,
                "tables": [],
            },
            schema="",
        )

        try:
            workspace = await Workspace.objects.aget(id=workspace_id)
        except Workspace.DoesNotExist:
            tc["result"] = not_provisioned
            return tc["result"]

        tenant_count = await workspace.tenants.acount()

        if tenant_count == 0:
            tc["result"] = not_provisioned
            return tc["result"]

        if tenant_count == 1:
            # Single-tenant: check TenantSchema directly
            tenant = await workspace.tenants.afirst()
            ts = await TenantSchema.objects.filter(
                tenant=tenant,
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            ).afirst()

            if ts is None:
                tc["result"] = not_provisioned
                return tc["result"]

            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=ts,
                    state=MaterializationRun.RunState.COMPLETED,
                )
                .order_by("-completed_at")
                .afirst()
            )

            last_materialized_at = None
            tables = []
            if last_run:
                if last_run.completed_at:
                    last_materialized_at = last_run.completed_at.isoformat()
                result_data = last_run.result or {}
                if "tables" in result_data:
                    tables = result_data["tables"]
                elif "table" in result_data and "rows_loaded" in result_data:
                    tables = [
                        {"name": result_data["table"], "row_count": result_data["rows_loaded"]}
                    ]

            tc["result"] = success_response(
                {
                    "exists": True,
                    "state": ts.state,
                    "last_materialized_at": last_materialized_at,
                    "tables": tables,
                },
                schema=ts.schema_name,
            )
            return tc["result"]

        # Multi-tenant: check WorkspaceViewSchema + per-tenant materialization
        vs = await WorkspaceViewSchema.objects.filter(
            workspace_id=workspace_id,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()

        if vs is None:
            tc["result"] = not_provisioned
            return tc["result"]

        # Collect last materialization time across all tenant schemas
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema__tenant_id__in=tenant_ids,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        last_materialized_at = None
        if last_run and last_run.completed_at:
            last_materialized_at = last_run.completed_at.isoformat()

        # List tables from the view schema via information_schema
        ctx = await _resolve_mcp_context(workspace_id)
        tables = await sync_to_async(workspace_list_tables)(ctx)

        tc["result"] = success_response(
            {
                "exists": True,
                "state": vs.state,
                "last_materialized_at": last_materialized_at,
                "tables": tables,
            },
            schema=vs.schema_name,
        )
        return tc["result"]


@mcp.tool()
async def teardown_schema(confirm: bool = False, workspace_id: str = "") -> dict:
    """Drop all materialized data for this workspace.

    Destructive — all tenant schemas and the workspace view schema are
    permanently dropped. Schemas will be re-provisioned automatically on
    the next materialization run. Metadata extracted during materialization
    (CommCare app structure, field definitions) is stored separately and
    is NOT affected.

    Only call this when the user explicitly requests a data reset, or when
    a failed materialization has left the schema in an unrecoverable state.

    Args:
        confirm: Must be True to execute. Defaults to False as a safety guard.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    from asgiref.sync import sync_to_async

    from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
    from apps.workspaces.services.schema_manager import SchemaManager

    async with tool_context("teardown_schema", workspace_id, confirm=confirm) as tc:
        if not confirm:
            tc["result"] = error_response(
                VALIDATION_ERROR,
                "Pass confirm=True to tear down the schema. "
                "This will permanently drop all materialized data.",
            )
            return tc["result"]

        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        from apps.workspaces.models import Workspace

        workspace = await Workspace.objects.filter(id=workspace_id).afirst()
        if workspace is None:
            tc["result"] = error_response(NOT_FOUND, f"Workspace '{workspace_id}' not found")
            return tc["result"]

        mgr = SchemaManager()
        dropped = []

        # Tear down the workspace view schema if it exists
        vs = (
            await WorkspaceViewSchema.objects.filter(
                workspace=workspace,
            )
            .exclude(state=SchemaState.TEARDOWN)
            .afirst()
        )
        if vs:
            await sync_to_async(mgr.teardown_view_schema)(vs)
            dropped.append(vs.schema_name)

        # Tear down all tenant schemas for this workspace
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        async for ts in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids,
        ).exclude(state=SchemaState.TEARDOWN):
            schema_name = ts.schema_name
            await sync_to_async(mgr.teardown)(ts)
            dropped.append(schema_name)

        tc["result"] = success_response(
            {"schemas_dropped": dropped},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


# --- Server setup ---


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # never write to stdout with stdio transport
    )


def _setup_django() -> None:
    """Initialize Django ORM for model access.

    Requires DJANGO_SETTINGS_MODULE to be set in the environment.
    Does NOT default to development settings to avoid accidentally
    running with DEBUG=True in production.
    """
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        raise RuntimeError(
            "DJANGO_SETTINGS_MODULE environment variable is required. "
            "Set it to 'config.settings.development' or 'config.settings.production'."
        )
    import django

    django.setup()


def _run_server(args: argparse.Namespace) -> None:
    """Start the MCP server (called directly or as a reload target)."""
    _configure_logging(args.verbose)
    _setup_django()

    logger.info("Starting Scout MCP server (transport=%s)", args.transport)

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # Allow internal Docker network hostname in addition to loopback defaults.
        # The MCP server is internal-only; DNS rebinding protection is still on.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "scout-mcp-web:*"],
        )

    mcp.run(transport=args.transport)


def _run_with_reload(args: argparse.Namespace) -> None:
    """Run the server in a subprocess and restart it when files change."""
    import subprocess

    from watchfiles import watch

    watch_dirs = ["mcp_server", "apps"]
    cmd = [
        sys.executable,
        "-m",
        "mcp_server",
        "--transport",
        args.transport,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.verbose:
        cmd.append("--verbose")

    _configure_logging(args.verbose)
    logger.info("Watching %s for changes (reload enabled)", ", ".join(watch_dirs))

    process = subprocess.Popen(cmd)
    try:
        for changes in watch(*watch_dirs, watch_filter=lambda _, path: path.endswith(".py")):
            changed = [str(c[1]) for c in changes]
            logger.info("Detected changes in %s — restarting", ", ".join(changed))
            process.terminate()
            process.wait()
            process = subprocess.Popen(cmd)
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()
        process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8100, help="HTTP port (default: 8100)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (development only)",
    )

    args = parser.parse_args()

    if args.reload:
        _run_with_reload(args)
    else:
        _run_server(args)


if __name__ == "__main__":
    main()
