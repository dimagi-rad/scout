"""
Query execution service for the MCP server.

Validates and executes read-only SQL against a tenant's database schema.
Agent-authored SQL goes through ``execute_query``, which enforces the
SQLValidator rules and row limits; backend-authored parameterized SQL goes
through ``execute_internal_query``. Both share the same pooled executor, so
both run under the tenant's read-only role.
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
from mcp_server.services.pool import get_pool
from mcp_server.services.sql_validator import SQLValidationError, SQLValidator

logger = logging.getLogger(__name__)


def _build_validator(ctx: QueryContext) -> SQLValidator:
    """Create a SQLValidator configured from the query context."""
    return SQLValidator(
        schema=ctx.schema_name,
        allowed_schemas=[],
        max_limit=ctx.max_rows_per_query,
    )


async def _execute_async_parameterized(
    ctx: QueryContext, sql: str, params: tuple, timeout_seconds: int
) -> dict[str, Any]:
    """Run a parameterized SQL query asynchronously. No validation or LIMIT injection.

    Uses the shared managed-DB pool (arch #253, 10#1). RESETs the connection's
    per-query state before returning it to the pool.
    """
    pool = await get_pool(ctx.connection_params)
    async with pool.connection() as conn, conn.cursor() as cursor:
        await cursor.execute(psql.SQL("SET ROLE {}").format(psql.Identifier(ctx.readonly_role)))
        try:
            await cursor.execute(
                psql.SQL("SET search_path TO {}").format(psql.Identifier(ctx.schema_name))
            )
            # Autocommit gives the query its own transaction; RESET ALL clears this default.
            await cursor.execute("SET default_transaction_read_only TO on")
            await cursor.execute(f"SET statement_timeout TO '{timeout_seconds}s'")
            # An empty tuple is not None, so psycopg would still scan the SQL for
            # placeholders and reject any literal '%' — which breaks the LIKE
            # '%term%' patterns agent SQL relies on (issue #406).
            await cursor.execute(sql, params or None)

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
            await cursor.execute("RESET ALL")


async def execute_internal_query(ctx: QueryContext, sql: str, params: tuple = ()) -> dict[str, Any]:
    """Execute a trusted internal query built by Scout backend code."""
    try:
        return await _execute_async_parameterized(ctx, sql, params, ctx.max_query_timeout_seconds)
    except Exception as e:
        code, message = _classify_error(e)
        logger.error("Internal query error: %s", message, exc_info=True)
        return error_response(code, message)


async def execute_query(ctx: QueryContext, sql: str) -> dict[str, Any]:
    """Validate and execute agent-authored SQL, returning a structured result dict."""
    validator = _build_validator(ctx)

    try:
        statement = validator.validate(sql)
    except SQLValidationError as e:
        logger.warning("SQL validation failed for tenant %s: %s", ctx.tenant_id, e.message)
        return error_response(VALIDATION_ERROR, e.message)
    except Exception:
        logger.warning(
            "SQL validation failed unexpectedly for tenant %s", ctx.tenant_id, exc_info=True
        )
        return error_response(
            VALIDATION_ERROR,
            "Could not validate supported PostgreSQL syntax. Simplify the query and retry.",
        )

    tables_accessed = validator.get_tables_accessed(statement)

    requested_limit = validator.limit_value(statement)
    try:
        sql_executed = validator.inject_limit(statement).sql(dialect=validator.dialect)
    except Exception:
        # SQLGlot can parse syntax that its PostgreSQL generator cannot render.
        logger.warning("SQL generation failed for tenant %s", ctx.tenant_id, exc_info=True)
        return error_response(
            VALIDATION_ERROR,
            "Could not generate supported PostgreSQL syntax. Simplify the query and retry.",
        )

    truncated = requested_limit is not None and requested_limit > validator.max_limit

    try:
        result = await _execute_async_parameterized(
            ctx, sql_executed, (), ctx.max_query_timeout_seconds
        )
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

    sqlstate = getattr(exc, "sqlstate", None) or ""
    if sqlstate[:2] in {"42", "22", "21"} or sqlstate == "0A000":
        return VALIDATION_ERROR, f"Invalid SQL query: {exc}. Reformulate the query and retry."

    if isinstance(exc, psycopg.Error):
        msg = str(exc)
        if "password authentication failed" in msg.lower():
            return (
                CONNECTION_ERROR,
                "Database authentication failed. Please contact an administrator.",
            )
        if "could not connect" in msg.lower():
            return CONNECTION_ERROR, "Could not connect to the database. Please try again later."
        return CONNECTION_ERROR, f"Query execution failed: {msg}"

    return INTERNAL_ERROR, "An unexpected error occurred while executing the query."
