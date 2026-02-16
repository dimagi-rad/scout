"""MCP tool for executing SQL queries against a Scout project database."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from apps.agents.tools.sql_tool import SQLExecutionResult, SQLValidator, SQLValidationError
from apps.projects.models import Project
from apps.projects.services.db_manager import get_pool_manager
from apps.projects.services.rate_limiter import RateLimitExceeded, get_rate_limiter

logger = logging.getLogger(__name__)


def register_sql_tool(mcp: FastMCP) -> None:
    """Register the execute_sql tool on the MCP server."""

    @mcp.tool()
    def execute_sql(project_slug: str, query: str) -> dict:
        """
        Execute a read-only SQL SELECT query against a Scout project's database.

        The query is validated for safety: only SELECT statements are allowed,
        dangerous functions are blocked, and results are automatically limited.

        Args:
            project_slug: The slug of the Scout project to query.
            query: The SQL SELECT query to execute.

        Returns:
            A dict with columns, rows, row_count, truncated, sql_executed,
            tables_accessed, caveats, and error (null on success).
        """
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return {"error": f"Project '{project_slug}' not found."}

        validator = SQLValidator(
            schema=project.db_schema,
            allowed_schemas=[],
            allowed_tables=project.allowed_tables or [],
            excluded_tables=project.excluded_tables or [],
            max_limit=project.max_rows_per_query,
        )

        result = SQLExecutionResult()

        # Validate
        try:
            statement = validator.validate(query)
        except SQLValidationError as e:
            result.error = e.message
            return result.to_dict()

        result.tables_accessed = validator.get_tables_accessed(statement)
        modified_statement = validator.inject_limit(statement)
        result.sql_executed = modified_statement.sql(dialect=validator.dialect)

        # Check truncation
        original_limit = statement.args.get("limit")
        if original_limit:
            original_limit_value = validator._get_limit_value(original_limit)
            if original_limit_value and original_limit_value > validator.max_limit:
                result.caveats.append(
                    f"Results limited to {validator.max_limit} rows "
                    f"(original query requested {original_limit_value})"
                )
                result.truncated = True

        # Rate limit (no user context in MCP — use None)
        try:
            get_rate_limiter().check_rate_limit(None, project)
        except RateLimitExceeded as e:
            result.error = str(e)
            return result.to_dict()

        # Execute
        import psycopg2
        import psycopg2.errors

        try:
            with get_pool_manager().get_connection(project) as conn:
                cursor = conn.cursor()
                try:
                    if project.db_schema:
                        cursor.execute(f"SET search_path TO {project.db_schema}")
                    cursor.execute(result.sql_executed)

                    if cursor.description:
                        result.columns = [desc[0] for desc in cursor.description]
                        result.rows = [list(row) for row in cursor.fetchall()]
                        result.row_count = len(result.rows)

                        if result.row_count == validator.max_limit:
                            result.truncated = True
                            if not any("limited to" in c for c in result.caveats):
                                result.caveats.append(
                                    f"Results may be truncated (returned exactly {validator.max_limit} rows)"
                                )
                finally:
                    cursor.close()

            get_rate_limiter().record_query(None, project)

        except psycopg2.errors.QueryCanceled:
            result.error = (
                f"Query timed out after {project.max_query_timeout_seconds} seconds. "
                "Consider adding filters or limiting the data range."
            )
        except psycopg2.Error as e:
            error_msg = str(e)
            if "password authentication failed" in error_msg.lower():
                result.error = "Database authentication failed."
            elif "could not connect" in error_msg.lower():
                result.error = "Could not connect to the database."
            elif "does not exist" in error_msg.lower():
                result.error = f"Database error: {error_msg}"
            else:
                result.error = f"Query execution failed: {error_msg}"
        except Exception:
            logger.exception("Unexpected error executing query for project %s", project_slug)
            result.error = "An unexpected error occurred while executing the query."

        return result.to_dict()
