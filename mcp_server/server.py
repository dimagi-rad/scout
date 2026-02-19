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
    """List tables in a tenant schema via information_schema."""
    from mcp_server.services.query import execute_internal_query

    result = await execute_internal_query(
        ctx,
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name",
        (ctx.db_schema,),
    )
    if not result.get("success", True) and "error" in result:
        return []

    tables = []
    for row in result.get("rows", []):
        tables.append({
            "name": row[0],
            "type": "view" if row[1] == "VIEW" else "table",
            "description": "",
        })
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
        (ctx.db_schema, table_name),
    )
    if not result.get("rows"):
        return None

    columns = []
    for row in result.get("rows", []):
        columns.append({
            "name": row[0],
            "type": row[1],
            "nullable": row[2] == "YES",
            "default": row[3],
        })
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
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        tables = await _tenant_list_tables(ctx)
        tc["result"] = success_response(
            {"tables": tables},
            tenant_id=tenant_id,
            schema=ctx.db_schema,
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
                NOT_FOUND, f"Table '{table_name}' not found in schema '{ctx.db_schema}'"
            )
            return tc["result"]

        tc["result"] = success_response(
            table,
            tenant_id=tenant_id,
            schema=ctx.db_schema,
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
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        tables_list = await _tenant_list_tables(ctx)
        tables = {}
        for t in tables_list:
            detail = await _tenant_describe_table(ctx, t["name"])
            if detail:
                tables[t["name"]] = detail

        tc["result"] = success_response(
            {"schema": ctx.db_schema, "table_count": len(tables), "tables": tables},
            tenant_id=tenant_id,
            schema=ctx.db_schema,
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
            schema=ctx.db_schema,
            timing_ms=tc["timer"].elapsed_ms,
            warnings=warnings or None,
        )
        return tc["result"]


@mcp.tool()
async def run_materialization(
    tenant_id: str, tenant_membership_id: str = "", pipeline: str = "commcare_sync",
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
                tm = await qs.aget(id=tenant_membership_id)
            else:
                tm = await qs.aget(
                    tenant_id=tenant_id, provider="commcare",
                )
        except TenantMembership.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Tenant '{tenant_id}' not found")
            return tc["result"]

        # Get OAuth token from the user's social account
        from allauth.socialaccount.models import SocialToken

        token_obj = await SocialToken.objects.filter(
            account__user=tm.user,
            account__provider__startswith="commcare",
        ).exclude(
            account__provider__startswith="commcare_connect",
        ).afirst()
        if not token_obj:
            tc["result"] = error_response(
                "AUTH_TOKEN_MISSING", "No CommCare OAuth token found"
            )
            return tc["result"]

        # Run materialization (sync, wrapped in sync_to_async)
        from asgiref.sync import sync_to_async

        from mcp_server.loaders.commcare_cases import CommCareAuthError
        from mcp_server.services.materializer import run_commcare_sync

        try:
            result = await sync_to_async(run_commcare_sync)(tm, token_obj.token)
        except CommCareAuthError as e:
            logger.warning("CommCare auth failed for tenant %s: %s", tenant_id, e)
            tc["result"] = error_response(
                AUTH_TOKEN_EXPIRED,
                str(e),
            )
            return tc["result"]
        except Exception as e:
            logger.exception("Materialization failed for tenant %s", tenant_id)
            tc["result"] = error_response(
                INTERNAL_ERROR, f"Materialization failed: {e}"
            )
            return tc["result"]

        tc["result"] = success_response(
            result,
            tenant_id=tenant_id,
            schema=result.get("schema", ""),
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

    args = parser.parse_args()

    _configure_logging(args.verbose)
    _setup_django()

    logger.info("Starting Scout MCP server (transport=%s)", args.transport)

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
