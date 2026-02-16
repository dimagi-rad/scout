"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

Every tool requires a project_id parameter identifying which project's
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

from mcp.server.fastmcp import FastMCP

from mcp_server.context import load_project_context
from mcp_server.envelope import (
    NOT_FOUND,
    VALIDATION_ERROR,
    error_response,
    success_response,
    tool_context,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


# --- Tools ---


@mcp.tool()
async def list_tables(project_id: str) -> dict:
    """List all tables and views in the project database.

    Returns table names, types (table/view), approximate row counts,
    and descriptions. Respects project-level table allow/exclude lists.

    Args:
        project_id: UUID of the Scout project to query.
    """
    async with tool_context("list_tables", project_id) as tc:
        try:
            ctx = await load_project_context(project_id)
        except ValueError as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from mcp_server.services import metadata

        tables = await metadata.list_tables(ctx.project_id)
        tc["result"] = success_response(
            {"tables": tables},
            project_id=ctx.project_id,
            schema=ctx.db_schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def describe_table(project_id: str, table_name: str) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default), primary keys,
    foreign key relationships, indexes, and semantic descriptions
    if available.

    Args:
        project_id: UUID of the Scout project to query.
        table_name: Name of the table to describe (case-insensitive).
    """
    async with tool_context("describe_table", project_id, table_name=table_name) as tc:
        try:
            ctx = await load_project_context(project_id)
        except ValueError as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from mcp_server.services import metadata

        table = await metadata.describe_table(ctx.project_id, table_name)
        if table is None:
            suggestions = await metadata.suggest_tables(ctx.project_id, table_name)
            tc["result"] = error_response(
                NOT_FOUND,
                f"Table '{table_name}' not found",
                detail=f"Did you mean: {', '.join(suggestions)}" if suggestions else None,
            )
            return tc["result"]

        tc["result"] = success_response(
            table,
            project_id=ctx.project_id,
            schema=ctx.db_schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_metadata(project_id: str) -> dict:
    """Get a complete metadata snapshot for the project database.

    Returns all tables, columns, relationships, and semantic layer
    information in a single call. Useful for building comprehensive
    understanding of available data.

    Args:
        project_id: UUID of the Scout project to query.
    """
    async with tool_context("get_metadata", project_id) as tc:
        try:
            ctx = await load_project_context(project_id)
        except ValueError as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        from mcp_server.services import metadata

        snapshot = await metadata.get_metadata(ctx.project_id)
        tc["result"] = success_response(
            snapshot,
            project_id=ctx.project_id,
            schema=ctx.db_schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def query(project_id: str, sql: str) -> dict:
    """Execute a read-only SQL query against the project database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        project_id: UUID of the Scout project to query.
        sql: A SQL SELECT query to execute.
    """
    async with tool_context("query", project_id, sql=sql) as tc:
        try:
            ctx = await load_project_context(project_id)
        except ValueError as e:
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
            warnings.append(
                f"Results truncated to {ctx.max_rows_per_query} rows"
            )

        tc["result"] = success_response(
            {
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result.get("truncated", False),
                "sql_executed": result.get("sql_executed", ""),
                "tables_accessed": result.get("tables_accessed", []),
            },
            project_id=ctx.project_id,
            schema=ctx.db_schema,
            timing_ms=tc["timer"].elapsed_ms,
            warnings=warnings or None,
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
    """Initialize Django ORM for model access."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
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

    kwargs: dict = {"transport": args.transport}
    if args.transport == "streamable-http":
        kwargs["host"] = args.host
        kwargs["port"] = args.port

    mcp.run(**kwargs)


if __name__ == "__main__":
    main()
