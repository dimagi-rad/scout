"""
Query execution service for the MCP server.

Executes trusted, backend-authored parameterized SQL against a tenant's
database schema. User and agent-authored SQL is not accepted here.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import psycopg.errors
from psycopg import sql as psql

from mcp_server.context import QueryContext
from mcp_server.envelope import (
    CONNECTION_ERROR,
    INTERNAL_ERROR,
    QUERY_TIMEOUT,
    VALIDATION_ERROR,
    error_response,
)

logger = logging.getLogger(__name__)


async def _execute_async_parameterized(
    ctx: QueryContext, sql: str, params: tuple, timeout_seconds: int
) -> dict[str, Any]:
    """Run a trusted parameterized SQL query asynchronously under the read-only role."""
    async with (
        await psycopg.AsyncConnection.connect(**ctx.connection_params, autocommit=True) as conn,
        conn.cursor() as cursor,
    ):
        await cursor.execute(psql.SQL("SET ROLE {}").format(psql.Identifier(ctx.readonly_role)))
        try:
            await cursor.execute(
                psql.SQL("SET search_path TO {}").format(psql.Identifier(ctx.schema_name))
            )
            await cursor.execute(f"SET statement_timeout TO '{timeout_seconds}s'")
            await cursor.execute(sql, params)

            columns: list[str] = []
            rows: list[list[Any]] = []

            if cursor.description:
                columns = [desc[0] for desc in cursor.description]
                rows = [list(row) for row in await cursor.fetchall()]

            return {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
            }
        finally:
            await cursor.execute("RESET ROLE")


async def execute_internal_query(ctx: QueryContext, sql: str, params: tuple = ()) -> dict[str, Any]:
    """Execute a trusted internal query built by Scout backend code."""
    try:
        return await _execute_async_parameterized(ctx, sql, params, ctx.max_query_timeout_seconds)
    except Exception as e:
        code, message = _classify_error(e)
        logger.error("Internal query error: %s", message, exc_info=True)
        return error_response(code, message)


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify a database exception into an error code and user-safe message."""
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return QUERY_TIMEOUT, "Query timed out. Consider adding filters or limiting the data range."

    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return (
            CONNECTION_ERROR,
            "Schema configuration error. Please contact an administrator.",
        )

    if isinstance(exc, psycopg.Error):
        msg = str(exc)
        if "password authentication failed" in msg.lower():
            return (
                CONNECTION_ERROR,
                "Database authentication failed. Please contact an administrator.",
            )
        if "could not connect" in msg.lower():
            return CONNECTION_ERROR, "Could not connect to the database. Please try again later."
        if "does not exist" in msg.lower():
            return VALIDATION_ERROR, f"Database error: {msg}"
        return CONNECTION_ERROR, f"Query execution failed: {msg}"

    return INTERNAL_ERROR, "An unexpected error occurred while executing the query."
