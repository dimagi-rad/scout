"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

Every tool requires a tenant_id parameter identifying which tenant's
database to operate on. All responses use a consistent envelope format.

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

from apps.projects.models import MaterializationRun, TenantMetadata, TenantSchema
from mcp_server.context import load_tenant_context
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
)

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


# --- Tools ---


@mcp.tool()
async def list_tables(tenant_id: str) -> dict:
    """List all tables in the tenant's database schema.

    Returns table names, types, descriptions, row counts, and materialization timestamps.
    Returns an empty list if no materialization run has completed yet.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    async with tool_context("list_tables", tenant_id) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"tables": [], "note": None},
                tenant_id=tenant_id,
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
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def describe_table(tenant_id: str, table_name: str) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default, description) and a table description.
    JSONB columns are annotated with summaries from the CommCare discover phase when available.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
        table_name: Name of the table to describe.
    """
    async with tool_context("describe_table", tenant_id, table_name=table_name) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
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
                tenant_membership_id=ts.tenant_membership_id
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
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_metadata(tenant_id: str) -> dict:
    """Get a complete metadata snapshot for the tenant's database.

    Returns all tables with their columns, descriptions, and table relationships
    defined by the materialization pipeline.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    async with tool_context("get_metadata", tenant_id) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"schema": ctx.schema_name, "table_count": 0, "tables": {}, "relationships": []},
                tenant_id=tenant_id,
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
            tenant_membership_id=ts.tenant_membership_id
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
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def query(tenant_id: str, sql: str) -> dict:
    """Execute a read-only SQL query against the tenant's database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
        sql: A SQL SELECT query to execute.
    """
    async with tool_context("query", tenant_id, sql=sql) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from mcp_server.services.query import execute_query

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
            tenant_id=tenant_id,
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
            run = await MaterializationRun.objects.select_related(
                "tenant_schema__tenant_membership"
            ).aget(id=run_id)
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        tenant_id = run.tenant_schema.tenant_membership.tenant_id
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
            tenant_id=tenant_id,
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
            run = await MaterializationRun.objects.select_related(
                "tenant_schema__tenant_membership"
            ).aget(id=run_id)
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

        tenant_id = run.tenant_schema.tenant_membership.tenant_id
        schema = run.tenant_schema.schema_name
        logger.info("Cancelled run %s for tenant %s (was: %s)", run_id, tenant_id, previous_state)

        tc["result"] = success_response(
            {"run_id": run_id, "cancelled": True, "previous_state": previous_state},
            tenant_id=tenant_id,
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def run_materialization(
    tenant_id: str,
    tenant_membership_id: str = "",
    pipeline: str = "commcare_sync",
    ctx: Context | None = None,
) -> dict:
    """Materialize data from a provider into the tenant's schema.

    Runs a three-phase pipeline (Discover → Load → Transform). Creates the schema
    automatically if it doesn't exist. Streams progress via MCP notifications/progress
    when the caller provides a progressToken.

    Args:
        tenant_id: The tenant identifier (domain or opportunity slug).
        tenant_membership_id: UUID of the specific TenantMembership to use.
        pipeline: Pipeline to run (default: commcare_sync).
    """
    from apps.users.models import TenantCredential, TenantMembership
    from mcp_server.loaders.commcare_base import CommCareAuthError
    from mcp_server.loaders.connect_base import ConnectAuthError

    async with tool_context("run_materialization", tenant_id, pipeline=pipeline) as tc:
        # ── Resolve pipeline config ───────────────────────────────────────────
        registry = get_registry()
        pipeline_config = registry.get(pipeline)
        if pipeline_config is None:
            tc["result"] = error_response(NOT_FOUND, f"Pipeline '{pipeline}' not found in registry")
            return tc["result"]

        # ── Resolve TenantMembership ──────────────────────────────────────────
        try:
            qs = TenantMembership.objects.select_related("user")
            tm = (
                await qs.aget(id=tenant_membership_id, tenant_id=tenant_id)
                if tenant_membership_id
                else await qs.aget(
                    tenant_id=tenant_id, provider=pipeline_config.provider
                )
            )
        except TenantMembership.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Tenant '{tenant_id}' not found")
            return tc["result"]

        # ── Resolve credential ────────────────────────────────────────────────
        try:
            cred_obj = await TenantCredential.objects.select_related("tenant_membership").aget(
                tenant_membership=tm
            )
        except TenantCredential.DoesNotExist:
            tc["result"] = error_response(
                "AUTH_TOKEN_MISSING", "No credential configured for this tenant"
            )
            return tc["result"]

        if cred_obj.credential_type == TenantCredential.API_KEY:
            from apps.users.adapters import decrypt_credential

            try:
                decrypted = await sync_to_async(decrypt_credential)(cred_obj.encrypted_credential)
            except Exception:
                logger.exception("Failed to decrypt API key for tenant %s", tenant_id)
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "Failed to decrypt API key")
                return tc["result"]
            credential = {"type": "api_key", "value": decrypted}
        else:
            from allauth.socialaccount.models import SocialToken

            if tm.provider == "commcare_connect":
                token_obj = await SocialToken.objects.filter(
                    account__user=tm.user,
                    account__provider__startswith="commcare_connect",
                ).afirst()
            else:
                token_obj = (
                    await SocialToken.objects.filter(
                        account__user=tm.user,
                        account__provider__startswith="commcare",
                    )
                    .exclude(account__provider__startswith="commcare_connect")
                    .afirst()
                )
            if not token_obj:
                tc["result"] = error_response(
                    "AUTH_TOKEN_MISSING",
                    f"No OAuth token found for provider '{tm.provider}'",
                )
                return tc["result"]
            credential = {"type": "oauth", "value": token_obj.token}

        # ── Build progress callback ───────────────────────────────────────────
        # run_pipeline runs in a thread (via sync_to_async), so we bridge back
        # to the async event loop with run_coroutine_threadsafe.
        # A done-callback logs any silent delivery failures.
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
            result = await sync_to_async(run_pipeline)(
                tm, credential, pipeline_config, progress_callback
            )
        except (CommCareAuthError, ConnectAuthError) as e:
            logger.warning("Auth failed for tenant %s: %s", tenant_id, e)
            tc["result"] = error_response(AUTH_TOKEN_EXPIRED, str(e))
            return tc["result"]
        except Exception:
            logger.exception("Pipeline '%s' failed for tenant %s", pipeline, tenant_id)
            tc["result"] = error_response(INTERNAL_ERROR, f"Pipeline '{pipeline}' failed")
            return tc["result"]

        tc["result"] = success_response(
            result,
            tenant_id=tenant_id,
            schema=result.get("schema", ""),
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_schema_status(tenant_id: str) -> dict:
    """Check whether data has been loaded for this tenant.

    Returns schema existence, state, last materialization timestamp, and table
    list. Always succeeds — returns exists=False if no schema has been
    provisioned yet. Safe to call before any data has been loaded.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    from apps.projects.models import MaterializationRun, SchemaState, TenantSchema

    async with tool_context("get_schema_status", tenant_id) as tc:
        ts = await TenantSchema.objects.filter(
            tenant_membership__tenant_id=tenant_id,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()

        if ts is None:
            tc["result"] = success_response(
                {
                    "exists": False,
                    "state": "not_provisioned",
                    "last_materialized_at": None,
                    "tables": [],
                },
                tenant_id=tenant_id,
                schema="",
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

        last_materialized_at = None
        tables = []
        if last_run:
            if last_run.completed_at:
                last_materialized_at = last_run.completed_at.isoformat()
            result_data = last_run.result or {}
            # Single-table envelope: {"table": "...", "rows_loaded": N}.
            # Multi-table pipelines may use a "tables" key instead; handle both.
            if "tables" in result_data:
                tables = result_data["tables"]
            elif "table" in result_data and "rows_loaded" in result_data:
                tables = [{"name": result_data["table"], "row_count": result_data["rows_loaded"]}]

        tc["result"] = success_response(
            {
                "exists": True,
                "state": ts.state,
                "last_materialized_at": last_materialized_at,
                "tables": tables,
            },
            tenant_id=tenant_id,
            schema=ts.schema_name,
        )
        return tc["result"]


@mcp.tool()
async def teardown_schema(tenant_id: str, confirm: bool = False) -> dict:
    """Drop the tenant's schema and all its materialized data.

    Destructive — the schema and all tables are permanently dropped. The
    schema will be re-provisioned automatically on the next materialization run.
    Metadata extracted during materialization (CommCare app structure, field
    definitions) is stored separately and is NOT affected.

    Only call this when the user explicitly requests a data reset, or when
    a failed materialization has left the schema in an unrecoverable state.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
        confirm: Must be True to execute. Defaults to False as a safety guard.
    """
    from asgiref.sync import sync_to_async

    from apps.projects.models import SchemaState, TenantSchema
    from apps.projects.services.schema_manager import SchemaManager

    async with tool_context("teardown_schema", tenant_id, confirm=confirm) as tc:
        if not confirm:
            tc["result"] = error_response(
                VALIDATION_ERROR,
                "Pass confirm=True to tear down the schema. "
                "This will permanently drop all materialized data for this tenant.",
            )
            return tc["result"]

        ts = (
            await TenantSchema.objects.filter(
                tenant_membership__tenant_id=tenant_id,
            )
            .exclude(state=SchemaState.TEARDOWN)
            .afirst()
        )

        if ts is None:
            tc["result"] = error_response(
                NOT_FOUND, f"No active schema found for tenant '{tenant_id}'"
            )
            return tc["result"]

        schema_name = ts.schema_name
        mgr = SchemaManager()
        await sync_to_async(mgr.teardown)(ts)

        tc["result"] = success_response(
            {"schema_dropped": schema_name},
            tenant_id=tenant_id,
            schema=schema_name,
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
