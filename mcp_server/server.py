"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

Every tool requires a project_id parameter identifying which project's
database to operate on.

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
    from mcp_server.services import metadata

    try:
        ctx = await load_project_context(project_id)
    except ValueError as e:
        return {"error": str(e)}

    tables = await metadata.list_tables(ctx.project_id)
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        "tables": tables,
    }


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
    from mcp_server.services import metadata

    try:
        ctx = await load_project_context(project_id)
    except ValueError as e:
        return {"error": str(e)}

    table = await metadata.describe_table(ctx.project_id, table_name)
    if table is None:
        suggestions = await metadata.suggest_tables(ctx.project_id, table_name)
        return {
            "error": f"Table '{table_name}' not found",
            "suggestions": suggestions,
        }
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        **table,
    }


@mcp.tool()
async def get_metadata(project_id: str) -> dict:
    """Get a complete metadata snapshot for the project database.

    Returns all tables, columns, relationships, and semantic layer
    information in a single call. Useful for building comprehensive
    understanding of available data.

    Args:
        project_id: UUID of the Scout project to query.
    """
    from mcp_server.services import metadata

    try:
        ctx = await load_project_context(project_id)
    except ValueError as e:
        return {"error": str(e)}

    snapshot = await metadata.get_metadata(ctx.project_id)
    return {
        "project_id": ctx.project_id,
        **snapshot,
    }


@mcp.tool()
async def query(project_id: str, sql: str) -> dict:
    """Execute a read-only SQL query against the project database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        project_id: UUID of the Scout project to query.
        sql: A SQL SELECT query to execute.
    """
    from mcp_server.services.query import execute_query

    try:
        ctx = await load_project_context(project_id)
    except ValueError as e:
        return {"error": str(e)}

    result = await execute_query(ctx, sql)
    return {"project_id": ctx.project_id, "schema": ctx.db_schema, **result}


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
