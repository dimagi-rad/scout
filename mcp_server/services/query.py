"""
Query execution service for the MCP server.

Validates and executes read-only SQL queries against a tenant's database schema.
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
from mcp_server.services.sql_validator import SQLValidationError, SQLValidator

logger = logging.getLogger(__name__)


def _build_validator(ctx: QueryContext) -> SQLValidator:
    """Create a SQLValidator configured from the query context."""
    return SQLValidator(
        schema=ctx.schema_name,
        allowed_schemas=[],
        max_limit=ctx.max_rows_per_query,
    )


async def _execute_async(ctx: QueryContext, sql: str, timeout_seconds: int) -> dict[str, Any]:
    """Run a SQL query asynchronously under the tenant's read-only role."""
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
            await cursor.execute(sql)

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


async def _execute_async_parameterized(
    ctx: QueryContext, sql: str, params: tuple, timeout_seconds: int
) -> dict[str, Any]:
    """Run a parameterized SQL query asynchronously. No validation or LIMIT injection."""
    async with (
        await psycopg.AsyncConnection.connect(**ctx.connection_params, autocommit=True) as conn,
        conn.cursor() as cursor,
    ):
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


async def execute_internal_query(ctx: QueryContext, sql: str, params: tuple = ()) -> dict[str, Any]:
    """Execute a trusted internal query, bypassing SQL validation."""
    try:
        return await _execute_async_parameterized(ctx, sql, params, ctx.max_query_timeout_seconds)
    except Exception as e:
        code, message = _classify_error(e)
        logger.error("Internal query error: %s", message, exc_info=True)
        return error_response(code, message)


async def execute_query(ctx: QueryContext, sql: str) -> dict[str, Any]:
    """Validate and execute a SQL query, returning a structured result dict."""
    validator = _build_validator(ctx)

    try:
        statement = validator.validate(sql)
    except SQLValidationError as e:
        logger.warning("SQL validation failed for tenant %s: %s", ctx.tenant_id, e.message)
        return error_response(VALIDATION_ERROR, e.message)

    tables_accessed = validator.get_tables_accessed(statement)

    modified = validator.inject_limit(statement)
    sql_executed = modified.sql(dialect=validator.dialect)

    truncated = False
    original_limit = statement.args.get("limit")
    if original_limit:
        limit_val = validator._get_limit_value(original_limit)
        if limit_val and limit_val > validator.max_limit:
            truncated = True

    try:
        result = await _execute_async(ctx, sql_executed, ctx.max_query_timeout_seconds)
    except Exception as e:
        code, message = _classify_error(e)
        logger.error("Query error for tenant %s: %s", ctx.tenant_id, message, exc_info=True)
        return error_response(code, message)

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
