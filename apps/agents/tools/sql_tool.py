"""
SQL validation and execution tool for the Scout data agent platform.

This module provides:
- SQLValidationError: Exception for SQL validation failures
- SQLValidator: Validates and sanitizes SQL queries
- create_sql_tool: Factory function to create a LangChain tool for SQL execution

Security features:
- Only SELECT statements allowed
- Single statement enforcement
- Dangerous function blocking
- Schema/table allowlist enforcement
- Automatic LIMIT injection
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import sqlglot
from langchain_core.tools import tool
from sqlglot import exp

if TYPE_CHECKING:
    from apps.projects.models import Project

logger = logging.getLogger(__name__)


# Dangerous PostgreSQL functions that could be used for data exfiltration or system access
DANGEROUS_FUNCTIONS: frozenset[str] = frozenset({
    # File system access
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_ls_archive_statusdir",
    "pg_ls_tmpdir",
    # Large object manipulation
    "lo_import",
    "lo_export",
    "lo_create",
    "lo_open",
    "lo_write",
    "lo_read",
    "lo_unlink",
    # Remote database access
    "dblink",
    "dblink_connect",
    "dblink_connect_u",
    "dblink_disconnect",
    "dblink_exec",
    "dblink_open",
    "dblink_fetch",
    "dblink_close",
    "dblink_get_connections",
    "dblink_send_query",
    "dblink_is_busy",
    "dblink_get_result",
    "dblink_cancel_query",
    "dblink_error_message",
    # Copy commands via functions
    "pg_copy_from",
    "pg_copy_to",
    # Extension management
    "pg_extension_config_dump",
    # Advisory locks (potential for DoS)
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    # System information that could aid attacks
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_terminate_backend",
    "pg_cancel_backend",
    # Command execution
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "cursor_to_xml",
    "cursor_to_xmlschema",
    "table_to_xml",
    "table_to_xmlschema",
    "table_to_xml_and_xmlschema",
    "schema_to_xml",
    "schema_to_xmlschema",
    "schema_to_xml_and_xmlschema",
    "database_to_xml",
    "database_to_xmlschema",
    "database_to_xml_and_xmlschema",
})

# Statement types that are not allowed (only SELECT is permitted)
FORBIDDEN_STATEMENT_TYPES: frozenset[type] = frozenset({
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Create,
    exp.Grant,
    exp.Revoke,
    exp.Merge,
    exp.Set,
    exp.Command,
})


class SQLValidationError(Exception):
    """
    Raised when SQL validation fails.

    Attributes:
        message: Human-readable error message
        sql: The SQL query that failed validation
        error_type: Category of validation error
    """

    def __init__(
        self,
        message: str,
        sql: str | None = None,
        error_type: str = "validation_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.sql = sql
        self.error_type = error_type

    def __str__(self) -> str:
        return self.message


@dataclass
class SQLValidator:
    """
    Validates SQL queries for safety and compliance with project rules.

    This validator ensures that:
    1. Only SELECT statements are executed
    2. Only a single statement is present
    3. No dangerous functions are called
    4. Only allowed tables/schemas are accessed

    Attributes:
        schema: The database schema to enforce (e.g., "public")
        allowed_tables: Tables that can be queried (empty = all tables allowed)
        excluded_tables: Tables that cannot be queried
        max_limit: Maximum number of rows to return
        dialect: SQL dialect for parsing (default: postgres)
    """

    schema: str = "public"
    allowed_tables: list[str] = field(default_factory=list)
    excluded_tables: list[str] = field(default_factory=list)
    max_limit: int = 500
    dialect: str = "postgres"

    def validate(self, sql: str) -> exp.Expression:
        """
        Validate a SQL query and return the parsed AST.

        Args:
            sql: The SQL query string to validate

        Returns:
            The parsed SQL expression (AST)

        Raises:
            SQLValidationError: If the query fails any validation check
        """
        # Parse the SQL into an AST
        try:
            statements = sqlglot.parse(sql, dialect=self.dialect)
        except sqlglot.errors.ParseError as e:
            raise SQLValidationError(
                f"SQL parse error: {e}",
                sql=sql,
                error_type="parse_error",
            ) from e

        # Check for empty or multiple statements
        if not statements:
            raise SQLValidationError(
                "Empty SQL statement",
                sql=sql,
                error_type="empty_statement",
            )

        # Filter out None values (can occur with trailing semicolons)
        valid_statements = [s for s in statements if s is not None]

        if len(valid_statements) == 0:
            raise SQLValidationError(
                "Empty SQL statement",
                sql=sql,
                error_type="empty_statement",
            )

        if len(valid_statements) > 1:
            raise SQLValidationError(
                "Multiple SQL statements are not allowed. Please submit one query at a time.",
                sql=sql,
                error_type="multiple_statements",
            )

        statement = valid_statements[0]

        # Check statement type - only SELECT allowed
        self._validate_statement_type(statement, sql)

        # Check for dangerous functions
        self._validate_no_dangerous_functions(statement, sql)

        # Check table access permissions
        self._validate_table_access(statement, sql)

        return statement

    def _validate_statement_type(self, statement: exp.Expression, sql: str) -> None:
        """Ensure only SELECT statements are allowed."""
        # Check if it's a SELECT statement
        if not isinstance(statement, exp.Select):
            # Also allow UNION, INTERSECT, EXCEPT which wrap SELECT statements
            if isinstance(statement, (exp.Union, exp.Intersect, exp.Except)):
                # These are valid compound SELECT operations
                return

            # Check for forbidden statement types
            for forbidden_type in FORBIDDEN_STATEMENT_TYPES:
                if isinstance(statement, forbidden_type):
                    raise SQLValidationError(
                        f"{forbidden_type.__name__.upper()} statements are not allowed. "
                        "Only SELECT queries are permitted.",
                        sql=sql,
                        error_type="forbidden_statement",
                    )

            # If not a SELECT and not explicitly forbidden, still reject
            raise SQLValidationError(
                f"Statement type '{type(statement).__name__}' is not allowed. "
                "Only SELECT queries are permitted.",
                sql=sql,
                error_type="forbidden_statement",
            )

    def _validate_no_dangerous_functions(
        self, statement: exp.Expression, sql: str
    ) -> None:
        """Check for dangerous function calls in the query."""
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""
            if func_name in DANGEROUS_FUNCTIONS:
                raise SQLValidationError(
                    f"Function '{func_name}' is not allowed for security reasons.",
                    sql=sql,
                    error_type="dangerous_function",
                )

        # Also check for Anonymous functions (raw function calls)
        for anon in statement.find_all(exp.Anonymous):
            func_name = anon.name.lower() if anon.name else ""
            if func_name in DANGEROUS_FUNCTIONS:
                raise SQLValidationError(
                    f"Function '{func_name}' is not allowed for security reasons.",
                    sql=sql,
                    error_type="dangerous_function",
                )

    def _validate_table_access(self, statement: exp.Expression, sql: str) -> None:
        """Validate that only allowed tables are accessed."""
        tables_accessed = self._extract_tables(statement)

        for table_info in tables_accessed:
            table_name = table_info["table"]
            table_schema = table_info.get("schema")

            # Check if table is in excluded list
            if table_name.lower() in {t.lower() for t in self.excluded_tables}:
                raise SQLValidationError(
                    f"Access to table '{table_name}' is not permitted.",
                    sql=sql,
                    error_type="table_not_allowed",
                )

            # If allowed_tables is specified, check if table is in the list
            if self.allowed_tables:
                allowed_lower = {t.lower() for t in self.allowed_tables}
                if table_name.lower() not in allowed_lower:
                    raise SQLValidationError(
                        f"Access to table '{table_name}' is not permitted. "
                        f"Allowed tables: {', '.join(self.allowed_tables)}",
                        sql=sql,
                        error_type="table_not_allowed",
                    )

            # Validate schema if specified in the query
            if table_schema and table_schema.lower() not in ("public", self.schema.lower()):
                raise SQLValidationError(
                    f"Access to schema '{table_schema}' is not permitted. "
                    f"Only schema '{self.schema}' is accessible.",
                    sql=sql,
                    error_type="schema_not_allowed",
                )

    def _extract_tables(self, statement: exp.Expression) -> list[dict[str, str]]:
        """
        Extract all table references from a SQL statement.

        Excludes CTE (Common Table Expression) aliases from the result since
        they are not actual database tables.

        Returns:
            List of dicts with 'table' and optional 'schema' keys
        """
        tables: list[dict[str, str]] = []

        # Collect CTE aliases to exclude them from table references
        cte_aliases: set[str] = set()
        for cte in statement.find_all(exp.CTE):
            if cte.alias:
                cte_aliases.add(cte.alias.lower())

        for table in statement.find_all(exp.Table):
            table_name = table.name
            # Skip CTE aliases - they're not real tables
            if table_name.lower() in cte_aliases:
                continue
            table_info: dict[str, str] = {"table": table_name}
            if table.db:
                table_info["schema"] = table.db
            tables.append(table_info)

        return tables

    def inject_limit(self, statement: exp.Expression) -> exp.Expression:
        """
        Add or cap the LIMIT clause on a SELECT statement.

        If no LIMIT exists, adds one with max_limit.
        If a LIMIT exists but exceeds max_limit, caps it at max_limit.
        If a LIMIT exists but cannot be parsed as a literal (e.g., subquery or expression),
        it is replaced with max_limit to prevent unlimited queries.

        Args:
            statement: The parsed SQL expression

        Returns:
            The modified expression with appropriate LIMIT
        """
        # Handle compound queries (UNION, INTERSECT, EXCEPT)
        if isinstance(statement, (exp.Union, exp.Intersect, exp.Except)):
            # For compound queries, we need to wrap in a subquery or apply limit to outer
            # Get existing limit if any
            existing_limit = statement.args.get("limit")
            if existing_limit:
                limit_value = self._get_limit_value(existing_limit)
                if limit_value is None:
                    # Non-literal LIMIT (e.g., subquery, expression) - force cap
                    logger.warning(
                        "Non-literal LIMIT expression detected, forcing cap to %d",
                        self.max_limit,
                    )
                    statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_limit)))
                elif limit_value > self.max_limit:
                    statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_limit)))
            else:
                statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_limit)))
            return statement

        # Handle regular SELECT
        if isinstance(statement, exp.Select):
            existing_limit = statement.args.get("limit")
            if existing_limit:
                limit_value = self._get_limit_value(existing_limit)
                if limit_value is None:
                    # Non-literal LIMIT (e.g., subquery, expression) - force cap
                    logger.warning(
                        "Non-literal LIMIT expression detected, forcing cap to %d",
                        self.max_limit,
                    )
                    statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_limit)))
                elif limit_value > self.max_limit:
                    statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_limit)))
            else:
                statement = statement.limit(self.max_limit)

        return statement

    def _get_limit_value(self, limit_expr: exp.Limit) -> int | None:
        """Extract the numeric value from a LIMIT expression."""
        if limit_expr.expression:
            if isinstance(limit_expr.expression, exp.Literal):
                try:
                    return int(limit_expr.expression.this)
                except (ValueError, TypeError):
                    return None
        return None

    def get_tables_accessed(self, statement: exp.Expression) -> list[str]:
        """
        Get a list of table names accessed by the query.

        Args:
            statement: The parsed SQL expression

        Returns:
            List of table names (without schema prefix)
        """
        return [t["table"] for t in self._extract_tables(statement)]


@dataclass
class SQLExecutionResult:
    """
    Result of a SQL query execution.

    Attributes:
        columns: List of column names in the result
        rows: List of rows, where each row is a list of values
        row_count: Number of rows returned
        truncated: Whether the results were truncated due to limit
        sql_executed: The actual SQL that was executed (may differ from input)
        tables_accessed: List of tables accessed by the query
        metric_used: Name of metric if the query used a predefined metric
        knowledge_applied: Any business knowledge applied to the query
        caveats: List of warnings or notes about the results
        error: Error message if the query failed
    """

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    sql_executed: str = ""
    tables_accessed: list[str] = field(default_factory=list)
    metric_used: str | None = None
    knowledge_applied: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "sql_executed": self.sql_executed,
            "tables_accessed": self.tables_accessed,
            "metric_used": self.metric_used,
            "knowledge_applied": self.knowledge_applied,
            "caveats": self.caveats,
            "error": self.error,
        }


def create_sql_tool(project: Project):
    """
    Factory function to create a SQL execution tool for a specific project.

    The returned tool validates and executes SQL queries against the project's
    database with appropriate security restrictions.

    Args:
        project: The Project model instance containing database connection
                 settings and access restrictions.

    Returns:
        A LangChain tool function that executes SQL queries.

    Example:
        >>> from apps.projects.models import Project
        >>> project = Project.objects.get(slug="my-project")
        >>> sql_tool = create_sql_tool(project)
        >>> result = sql_tool.invoke({"query": "SELECT * FROM users LIMIT 10"})
    """
    # Create validator with project settings
    validator = SQLValidator(
        schema=project.db_schema,
        allowed_tables=project.allowed_tables or [],
        excluded_tables=project.excluded_tables or [],
        max_limit=project.max_rows_per_query,
    )

    @tool
    def execute_sql(query: str) -> dict[str, Any]:
        """
        Execute a SQL SELECT query against the project database.

        This tool validates the query for safety, enforces table access restrictions,
        and automatically limits result size. Only SELECT queries are allowed.

        Args:
            query: The SQL SELECT query to execute. Must be a single statement.
                   INSERT, UPDATE, DELETE, and other modification statements are
                   not allowed.

        Returns:
            A dictionary containing:
            - columns: List of column names
            - rows: List of result rows
            - row_count: Number of rows returned
            - truncated: Whether results were truncated due to limit
            - sql_executed: The actual SQL executed (may include injected LIMIT)
            - tables_accessed: Tables referenced in the query
            - caveats: Any warnings or notes about the results
            - error: Error message if the query failed (None on success)

        Example:
            >>> execute_sql("SELECT name, email FROM customers WHERE active = true")
            {
                "columns": ["name", "email"],
                "rows": [["John Doe", "john@example.com"], ...],
                "row_count": 42,
                "truncated": false,
                "sql_executed": "SELECT name, email FROM customers WHERE active = true LIMIT 500",
                "tables_accessed": ["customers"],
                "caveats": [],
                "error": null
            }
        """
        result = SQLExecutionResult()

        # Validate the query
        try:
            statement = validator.validate(query)
        except SQLValidationError as e:
            logger.warning(
                "SQL validation failed for project %s: %s",
                project.slug,
                e.message,
            )
            result.error = e.message
            return result.to_dict()

        # Extract tables before modifying the statement
        result.tables_accessed = validator.get_tables_accessed(statement)

        # Inject or cap LIMIT
        modified_statement = validator.inject_limit(statement)
        result.sql_executed = modified_statement.sql(dialect=validator.dialect)

        # Check if we're potentially truncating
        original_limit = statement.args.get("limit")
        if original_limit:
            original_limit_value = validator._get_limit_value(original_limit)
            if original_limit_value and original_limit_value > validator.max_limit:
                result.caveats.append(
                    f"Results limited to {validator.max_limit} rows "
                    f"(original query requested {original_limit_value})"
                )
                result.truncated = True

        # Execute the query
        conn = None
        cursor = None
        try:
            # Get connection parameters from project
            conn_params = project.get_connection_params()

            # Connect with psycopg2
            conn = psycopg2.connect(**conn_params)

            # Set connection to read-only mode
            conn.set_session(readonly=True)

            # Create cursor and execute
            cursor = conn.cursor()
            cursor.execute(result.sql_executed)

            # Fetch results
            if cursor.description:
                result.columns = [desc[0] for desc in cursor.description]
                result.rows = [list(row) for row in cursor.fetchall()]
                result.row_count = len(result.rows)

                # Check if we hit the limit (may indicate truncation)
                if result.row_count == validator.max_limit:
                    result.truncated = True
                    if not any("limited to" in c for c in result.caveats):
                        result.caveats.append(
                            f"Results may be truncated (returned exactly {validator.max_limit} rows)"
                        )

            logger.info(
                "SQL query executed for project %s: %d rows returned",
                project.slug,
                result.row_count,
            )

        except psycopg2.errors.QueryCanceled as e:
            logger.warning(
                "Query timeout for project %s: %s",
                project.slug,
                str(e),
            )
            result.error = (
                f"Query timed out after {project.max_query_timeout_seconds} seconds. "
                "Consider adding filters or limiting the data range."
            )

        except psycopg2.Error as e:
            logger.error(
                "Database error for project %s: %s",
                project.slug,
                str(e),
                exc_info=True,
            )
            # Sanitize error message - don't expose internal details
            error_msg = str(e)
            if "password authentication failed" in error_msg.lower():
                result.error = "Database authentication failed. Please contact an administrator."
            elif "could not connect" in error_msg.lower():
                result.error = "Could not connect to the database. Please try again later."
            elif "does not exist" in error_msg.lower():
                # This is a user error (bad table/column name), safe to show
                result.error = f"Database error: {error_msg}"
            else:
                # Generic error for other cases
                result.error = f"Query execution failed: {error_msg}"

        except Exception:
            logger.exception(
                "Unexpected error executing query for project %s",
                project.slug,
            )
            result.error = "An unexpected error occurred while executing the query."

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

        return result.to_dict()

    # Set a descriptive name for the tool
    execute_sql.name = "execute_sql"

    return execute_sql


__all__ = [
    "SQLValidationError",
    "SQLValidator",
    "SQLExecutionResult",
    "create_sql_tool",
    "DANGEROUS_FUNCTIONS",
    "FORBIDDEN_STATEMENT_TYPES",
]
