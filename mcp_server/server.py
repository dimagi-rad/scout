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
import logging
import os
import sys

from django.core.exceptions import ValidationError as _ValidationError
from mcp.server.fastmcp import FastMCP

from mcp_server.context import load_tenant_context
from mcp_server.envelope import (
    AUTH_TOKEN_EXPIRED,
    NOT_FOUND,
    VALIDATION_ERROR,
    error_response,
    success_response,
    tool_context,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


# --- Tenant metadata helpers ---
# These query information_schema directly, bypassing the Project-based
# metadata service. Used for tenant contexts where there's no Project.


async def _tenant_list_tables(ctx) -> list[dict]:
    """List tables in a tenant schema via information_schema.

    Raises RuntimeError if the database query fails, so callers can
    return a proper error response rather than silently reporting an
    empty table list.
    """
    from mcp_server.services.query import execute_internal_query

    result = await execute_internal_query(
        ctx,
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name",
        (ctx.schema_name,),
    )
    if not result.get("success", True) and "error" in result:
        msg = result["error"].get("message", "Database query failed")
        raise RuntimeError(msg)

    tables = []
    for row in result.get("rows", []):
        tables.append(
            {
                "name": row[0],
                "type": "view" if row[1] == "VIEW" else "table",
                "description": "",
            }
        )
    return tables


async def _tenant_describe_table(ctx, table_name: str) -> dict | None:
    """Describe a table in a tenant schema via information_schema."""
    from mcp_server.services.query import execute_internal_query

    result = await execute_internal_query(
        ctx,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (ctx.schema_name, table_name),
    )
    if not result.get("rows"):
        return None

    columns = []
    for row in result.get("rows", []):
        columns.append(
            {
                "name": row[0],
                "type": row[1],
                "nullable": row[2] == "YES",
                "default": row[3],
            }
        )
    return {"name": table_name, "columns": columns}


# --- Tools ---


@mcp.tool()
async def list_tables(tenant_id: str) -> dict:
    """List all tables and views in the tenant's database schema.

    Returns table names, types (table/view), and descriptions.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    async with tool_context("list_tables", tenant_id) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
            tables = await _tenant_list_tables(ctx)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]
        except RuntimeError as e:
            tc["result"] = error_response("INTERNAL_ERROR", str(e))
            return tc["result"]

        tc["result"] = success_response(
            {"tables": tables},
            tenant_id=tenant_id,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def describe_table(tenant_id: str, table_name: str) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default).

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

        table = await _tenant_describe_table(ctx, table_name)
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

    Returns all tables with their columns.

    Args:
        tenant_id: The tenant identifier (e.g. CommCare domain name).
    """
    async with tool_context("get_metadata", tenant_id) as tc:
        try:
            ctx = await load_tenant_context(tenant_id)
            tables_list = await _tenant_list_tables(ctx)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]
        except RuntimeError as e:
            tc["result"] = error_response("INTERNAL_ERROR", str(e))
            return tc["result"]

        tables = {}
        for t in tables_list:
            detail = await _tenant_describe_table(ctx, t["name"])
            if detail:
                tables[t["name"]] = detail

        tc["result"] = success_response(
            {"schema": ctx.schema_name, "table_count": len(tables), "tables": tables},
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
async def run_materialization(
    tenant_id: str,
    tenant_membership_id: str = "",
    pipeline: str = "commcare_sync",
) -> dict:
    """Materialize data from CommCare into the tenant's schema.

    Loads case data from the CommCare API and writes it to the tenant's
    schema in the managed database. Creates the schema if it doesn't exist.

    Args:
        tenant_id: The tenant identifier (CommCare domain name).
        tenant_membership_id: UUID of the specific TenantMembership to use.
        pipeline: Pipeline to run (default: commcare_sync).
    """
    from mcp_server.envelope import INTERNAL_ERROR

    async with tool_context("run_materialization", tenant_id, pipeline=pipeline) as tc:
        from apps.users.models import TenantMembership

        try:
            qs = TenantMembership.objects.select_related("user")
            if tenant_membership_id:
                # Scope to tenant_id so a caller cannot use an arbitrary membership UUID
                # to trigger materialization with a different user's credentials.
                tm = await qs.aget(id=tenant_membership_id, tenant_id=tenant_id)
            else:
                tm = await qs.aget(
                    tenant_id=tenant_id,
                    provider="commcare",
                )
        except TenantMembership.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Tenant '{tenant_id}' not found")
            return tc["result"]

        # Get credential from TenantCredential (supports both OAuth and API key)
        from apps.users.models import TenantCredential

        try:
            cred_obj = await TenantCredential.objects.select_related("tenant_membership").aget(
                tenant_membership=tm
            )
        except TenantCredential.DoesNotExist:
            tc["result"] = error_response(
                "AUTH_TOKEN_MISSING", "No credential configured for this tenant"
            )
            return tc["result"]

        from asgiref.sync import sync_to_async

        if cred_obj.credential_type == TenantCredential.API_KEY:
            from apps.users.adapters import decrypt_credential

            try:
                decrypted = await sync_to_async(decrypt_credential)(cred_obj.encrypted_credential)
            except Exception:
                logger.exception(
                    "Failed to decrypt API key for tenant %s membership %s",
                    tenant_id,
                    cred_obj.id,
                )
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "Failed to decrypt API key")
                return tc["result"]
            credential = {"type": "api_key", "value": decrypted}
        else:
            # OAuth: retrieve from allauth SocialToken
            from allauth.socialaccount.models import SocialToken

            token_obj = (
                await SocialToken.objects.filter(
                    account__user=tm.user,
                    account__provider__startswith="commcare",
                )
                .exclude(account__provider__startswith="commcare_connect")
                .afirst()
            )
            if not token_obj:
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "No CommCare OAuth token found")
                return tc["result"]
            credential = {"type": "oauth", "value": token_obj.token}

        # Run materialization (sync, wrapped in sync_to_async)
        from mcp_server.loaders.commcare_cases import CommCareAuthError
        from mcp_server.services.materializer import run_commcare_sync

        try:
            result = await sync_to_async(run_commcare_sync)(tm, credential)
        except CommCareAuthError as e:
            logger.warning("CommCare auth failed for tenant %s: %s", tenant_id, e)
            tc["result"] = error_response(
                AUTH_TOKEN_EXPIRED,
                str(e),
            )
            return tc["result"]
        except Exception as e:
            logger.exception("Materialization failed for tenant %s", tenant_id)
            tc["result"] = error_response(INTERNAL_ERROR, f"Materialization failed: {e}")
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

        last_run = await MaterializationRun.objects.filter(
            tenant_schema=ts,
            state="completed",
        ).afirst()

        last_materialized_at = None
        tables = []
        if last_run:
            if last_run.completed_at:
                last_materialized_at = last_run.completed_at.isoformat()
            result_data = last_run.result or {}
            if "table" in result_data and "rows_loaded" in result_data:
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
