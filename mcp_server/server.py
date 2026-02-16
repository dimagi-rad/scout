"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

Usage:
    # stdio transport (for local clients)
    python -m mcp_server --project-id <uuid>

    # HTTP transport (for networked clients)
    python -m mcp_server --project-id <uuid> --transport streamable-http

    # Specify host/port for HTTP
    python -m mcp_server --project-id <uuid> --transport streamable-http --host 0.0.0.0 --port 9000
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


# --- v1 Tool stubs ---


@mcp.tool()
def list_tables() -> dict:
    """List all tables and views in the project database.

    Returns table names, types (table/view), approximate row counts,
    and descriptions. Respects project-level table allow/exclude lists.
    """
    ctx = get_project_context()
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        "tables": [],  # TODO: Milestone 2
    }


@mcp.tool()
def describe_table(table_name: str) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default), primary keys,
    foreign key relationships, indexes, and semantic descriptions
    if available.

    Args:
        table_name: Name of the table to describe.
    """
    ctx = get_project_context()
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        "table_name": table_name,
        "columns": [],  # TODO: Milestone 2
    }


@mcp.tool()
def get_metadata() -> dict:
    """Get a complete metadata snapshot for the project database.

    Returns all tables, columns, relationships, and semantic layer
    information in a single call. Useful for building comprehensive
    understanding of available data.
    """
    ctx = get_project_context()
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        "tables": {},  # TODO: Milestone 2
    }


@mcp.tool()
def query(sql: str) -> dict:
    """Execute a read-only SQL query against the project database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        sql: A SQL SELECT query to execute.
    """
    ctx = get_project_context()
    return {
        "project_id": ctx.project_id,
        "schema": ctx.db_schema,
        "columns": [],  # TODO: Milestone 3
        "rows": [],
        "row_count": 0,
    }


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


def _load_project(project_id: str) -> None:
    """Load a project from the database and set the global context."""
    from apps.projects.models import Project

    try:
        project = Project.objects.select_related("database_connection").get(
            id=project_id, is_active=True
        )
    except Project.DoesNotExist:
        logger.error("Project %s not found or not active", project_id)
        sys.exit(1)

    if not project.database_connection.is_active:
        logger.error(
            "Database connection '%s' for project '%s' is not active",
            project.database_connection.name,
            project.name,
        )
        sys.exit(1)

    ctx = ProjectContext.from_project(project)
    set_project_context(ctx)
    logger.info(
        "Loaded project '%s' (schema=%s, tables=%s)",
        ctx.project_name,
        ctx.db_schema,
        f"allow={ctx.allowed_tables}" if ctx.allowed_tables else "all",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout MCP Server")
    parser.add_argument(
        "--project-id",
        required=True,
        help="UUID of the Scout project to serve",
    )
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
    _load_project(args.project_id)

    logger.info("Starting Scout MCP server (transport=%s)", args.transport)

    kwargs: dict = {"transport": args.transport}
    if args.transport == "streamable-http":
        kwargs["host"] = args.host
        kwargs["port"] = args.port

    mcp.run(**kwargs)


if __name__ == "__main__":
    main()
