"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

The agent must call set_project with a project_id before using any other
tools. This sets the session context for all subsequent tool calls.

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

from mcp_server.context import ProjectContext, get_project_context, set_project_context

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


# --- Session setup ---


@mcp.tool()
async def set_project(project_id: str) -> dict:
    """Set the active project for this session.

    Must be called before using any other tools. Loads the project's
    database connection, schema configuration, and query limits.
    Can be called again to switch projects within the same session.

    Args:
        project_id: UUID of the Scout project to work with.
    """
    from apps.projects.models import Project

    try:
        project = await Project.objects.select_related("database_connection").aget(
            id=project_id, is_active=True
        )
    except Project.DoesNotExist:
        return {"error": f"Project '{project_id}' not found or not active"}

    if not project.database_connection.is_active:
        return {"error": f"Database connection for project '{project.name}' is not active"}

    ctx = ProjectContext.from_project(project)
    set_project_context(ctx)
    logger.info(
        "Session project set to '%s' (schema=%s)",
        ctx.project_name,
        ctx.db_schema,
    )
    return {
        "project_id": ctx.project_id,
        "project_name": ctx.project_name,
        "schema": ctx.db_schema,
        "max_rows_per_query": ctx.max_rows_per_query,
        "max_query_timeout_seconds": ctx.max_query_timeout_seconds,
    }


# --- v1 Tools ---


@mcp.tool()
async def list_tables() -> dict:
    """List all tables and views in the project database.

    Returns table names, types (table/view), approximate row counts,
    and descriptions. Respects project-level table allow/exclude lists.

    Requires set_project to be called first.
    """
    from mcp_server.services import metadata

    ctx = get_project_context()
    tables = await metadata.list_tables(ctx.project_id)
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        "tables": tables,
    }


@mcp.tool()
async def describe_table(table_name: str) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default), primary keys,
    foreign key relationships, indexes, and semantic descriptions
    if available.

    Requires set_project to be called first.

    Args:
        table_name: Name of the table to describe (case-insensitive).
    """
    from mcp_server.services import metadata

    ctx = get_project_context()
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
async def get_metadata() -> dict:
    """Get a complete metadata snapshot for the project database.

    Returns all tables, columns, relationships, and semantic layer
    information in a single call. Useful for building comprehensive
    understanding of available data.

    Requires set_project to be called first.
    """
    from mcp_server.services import metadata

    ctx = get_project_context()
    snapshot = await metadata.get_metadata(ctx.project_id)
    return {
        "project_id": ctx.project_id,
        **snapshot,
    }


@mcp.tool()
async def query(sql: str) -> dict:
    """Execute a read-only SQL query against the project database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Requires set_project to be called first.

    Args:
        sql: A SQL SELECT query to execute.
    """
    from mcp_server.services.query import execute_query

    ctx = get_project_context()
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
