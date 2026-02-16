"""
Query execution service for the MCP server.

Validates and executes read-only SQL queries against a project's database.
Reuses SQLValidator for safety checks and ConnectionPoolManager for
connection pooling. All sync psycopg2 work is wrapped with sync_to_async.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async

from apps.agents.tools.sql_tool import SQLValidationError, SQLValidator
from apps.projects.services.db_manager import get_pool_manager
from mcp_server.context import ProjectContext

logger = logging.getLogger(__name__)


def _build_validator(ctx: ProjectContext) -> SQLValidator:
    """Create a SQLValidator configured from the project context."""
    return SQLValidator(
        schema=ctx.db_schema,
        allowed_schemas=[],
        allowed_tables=ctx.allowed_tables,
        excluded_tables=ctx.excluded_tables,
        max_limit=ctx.max_rows_per_query,
    )


def _execute_sync(ctx: ProjectContext, sql: str, timeout_seconds: int) -> dict[str, Any]:
    """Run a SQL query synchronously using the connection pool.

    This function is meant to be called via sync_to_async.
    """
    from psycopg2 import sql as psql

    pool_mgr = get_pool_manager()

    # ConnectionPoolManager.get_connection expects a Project-like object with
    # .id and .get_connection_params(). Build a minimal shim from context.
    project_shim = _ProjectShim(ctx)

    with pool_mgr.get_connection(project_shim) as conn:
        cursor = conn.cursor()
        try:
            # Set search_path and statement timeout
            cursor.execute(
                psql.SQL("SET search_path TO {}").format(psql.Identifier(ctx.db_schema))
            )
            cursor.execute("SET statement_timeout TO %s", (f"{timeout_seconds}s",))

            cursor.execute(sql)

            columns: list[str] = []
            rows: list[list[Any]] = []

            if cursor.description:
                columns = [desc[0] for desc in cursor.description]
                rows = [list(row) for row in cursor.fetchall()]

            return {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
            }
        finally:
            cursor.close()


class _ProjectShim:
    """Minimal stand-in for a Project model, used by ConnectionPoolManager."""

    def __init__(self, ctx: ProjectContext) -> None:
        self.id = ctx.project_id
        self.slug = ctx.project_name  # used only for logging
        self._connection_params = ctx.connection_params

    def get_connection_params(self) -> dict[str, Any]:
        return self._connection_params


async def execute_query(ctx: ProjectContext, sql: str) -> dict[str, Any]:
    """Validate and execute a SQL query, returning a structured result dict.

    Returns a dict with keys:
        columns, rows, row_count, truncated, sql_executed, tables_accessed, error
    On validation or execution failure, only ``error`` is populated.
    """
    validator = _build_validator(ctx)

    # --- Validate ---
    try:
        statement = validator.validate(sql)
    except SQLValidationError as e:
        logger.warning("SQL validation failed for project %s: %s", ctx.project_name, e.message)
        return {"error": e.message}

    tables_accessed = validator.get_tables_accessed(statement)

    # --- Inject / cap LIMIT ---
    modified = validator.inject_limit(statement)
    sql_executed = modified.sql(dialect=validator.dialect)

    truncated = False
    original_limit = statement.args.get("limit")
    if original_limit:
        limit_val = validator._get_limit_value(original_limit)
        if limit_val and limit_val > validator.max_limit:
            truncated = True

    # --- Execute ---
    try:
        result = await sync_to_async(_execute_sync)(ctx, sql_executed, ctx.max_query_timeout_seconds)
    except Exception as e:
        error_msg = _sanitize_error(e)
        logger.error("Query error for project %s: %s", ctx.project_name, error_msg, exc_info=True)
        return {"error": error_msg}

    if result["row_count"] == validator.max_limit:
        truncated = True

    return {
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": truncated,
        "sql_executed": sql_executed,
        "tables_accessed": tables_accessed,
    }


def _sanitize_error(exc: Exception) -> str:
    """Return a user-safe error message for database exceptions."""
    import psycopg2
    import psycopg2.errors

    if isinstance(exc, psycopg2.errors.QueryCanceled):
        return "Query timed out. Consider adding filters or limiting the data range."

    if isinstance(exc, psycopg2.Error):
        msg = str(exc)
        if "password authentication failed" in msg.lower():
            return "Database authentication failed. Please contact an administrator."
        if "could not connect" in msg.lower():
            return "Could not connect to the database. Please try again later."
        if "does not exist" in msg.lower():
            return f"Database error: {msg}"
        return f"Query execution failed: {msg}"

    return "An unexpected error occurred while executing the query."
