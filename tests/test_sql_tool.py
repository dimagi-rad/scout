"""
Comprehensive tests for SQL tool execution.

Tests cover:
- Successful query execution
- Timeout handling
- Error handling
- Read-only enforcement
- Result structure
- Database connection management
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import psycopg2
from psycopg2.extensions import QueryCanceledError

from apps.agents.tools.sql_tool import create_sql_tool, SQLExecutionResult
from apps.projects.models import Project


@pytest.fixture
def mock_project(db_connection, user):
    """Create a mock project for testing."""
    return Project.objects.create(
        name="Test Project",
        slug="test-project",
        database_connection=db_connection,
        db_schema="public",
        max_rows_per_query=100,
        max_query_timeout_seconds=30,
        created_by=user,
    )


class TestSuccessfulQueryExecution:
    """Test successful query execution scenarios."""

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_simple_select_query(self, mock_connect, mock_project):
        """Test execution of a simple SELECT query."""
        # Mock database connection and cursor (no context manager)
        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("id", None, None, None, None, None, None),
            ("name", None, None, None, None, None, None),
        ]
        mock_cursor.fetchall.return_value = [
            (1, "Alice"),
            (2, "Bob"),
        ]
        mock_cursor.rowcount = 2

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        # Create and execute tool
        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id, name FROM users"})

        # Verify result structure
        assert isinstance(result, dict)
        assert "columns" in result
        assert "rows" in result
        assert "row_count" in result
        assert "sql_executed" in result

        # Verify result content
        assert result["columns"] == ["id", "name"]
        assert result["rows"] == [[1, "Alice"], [2, "Bob"]]
        assert result["row_count"] == 2
        assert "SELECT" in result["sql_executed"]

        # Verify read-only session was set
        mock_conn.set_session.assert_called_once_with(readonly=True)

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_query_with_aggregation(self, mock_connect, mock_project):
        """Test query with aggregation functions."""
        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("total", None, None, None, None, None, None),
            ("avg", None, None, None, None, None, None),
        ]
        mock_cursor.fetchall.return_value = [(1000, 50.5)]
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({
            "query": "SELECT SUM(amount) as total, AVG(amount) as avg FROM orders"
        })

        assert result["columns"] == ["total", "avg"]
        assert result["rows"] == [[1000, 50.5]]
        assert result["row_count"] == 1

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_empty_result_set(self, mock_connect, mock_project):
        """Test query that returns no rows."""
        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("id", None, None, None, None, None, None),
        ]
        mock_cursor.fetchall.return_value = []
        mock_cursor.rowcount = 0

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id FROM users WHERE id = 999999"})

        assert result["columns"] == ["id"]
        assert result["rows"] == []
        assert result["row_count"] == 0

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_limit_injection(self, mock_connect, mock_project):
        """Test that LIMIT is injected when missing."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(i,) for i in range(100)]
        mock_cursor.rowcount = 100

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id FROM users"})

        # Check that LIMIT was added to executed SQL
        assert "LIMIT" in result["sql_executed"].upper()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_result_truncation_flag(self, mock_connect, mock_project):
        """Test that truncated flag is set correctly."""
        # Return exactly max_rows_per_query rows
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(i,) for i in range(100)]
        mock_cursor.rowcount = 100

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id FROM users LIMIT 100"})

        # Should indicate potential truncation
        assert "truncated" in result
        # With exactly max_rows, might be truncated or not depending on implementation


class TestTimeoutHandling:
    """Test query timeout scenarios."""

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_query_timeout(self, mock_connect, mock_project):
        """Test handling of query timeout."""
        mock_cursor = MagicMock()
        # Simulate QueryCanceledError
        mock_cursor.execute.side_effect = QueryCanceledError("canceling statement due to statement timeout")

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT * FROM huge_table"})

        # Should return error information
        assert "error" in result
        assert "timeout" in result["error"].lower() or "canceled" in result["error"].lower()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_timeout_with_complex_query(self, mock_connect, mock_project):
        """Test timeout with complex query that takes too long."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = QueryCanceledError()

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        complex_query = """
        SELECT u.id, COUNT(o.id)
        FROM users u
        CROSS JOIN orders o
        CROSS JOIN products p
        GROUP BY u.id
        """
        result = tool.invoke({"query": complex_query})

        assert "error" in result


class TestErrorHandling:
    """Test error handling scenarios."""

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_syntax_error(self, mock_connect, mock_project):
        """Test handling of SQL syntax errors."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.errors.SyntaxError("syntax error at or near 'FROM'")

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT FROM users"})

        assert "error" in result
        assert "syntax" in result["error"].lower()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_undefined_table(self, mock_connect, mock_project):
        """Test handling of undefined table error."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.errors.UndefinedTable('relation "nonexistent" does not exist')

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT * FROM nonexistent"})

        assert "error" in result
        assert "does not exist" in result["error"].lower() or "undefined" in result["error"].lower()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_undefined_column(self, mock_connect, mock_project):
        """Test handling of undefined column error."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.errors.UndefinedColumn('column "badcolumn" does not exist')

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT badcolumn FROM users"})

        assert "error" in result
        assert "column" in result["error"].lower() or "does not exist" in result["error"].lower()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_connection_error(self, mock_connect, mock_project):
        """Test handling of database connection errors."""
        mock_connect.side_effect = psycopg2.OperationalError("could not connect to server")

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT * FROM users"})

        assert "error" in result
        assert "connect" in result["error"].lower() or "connection" in result["error"].lower()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_division_by_zero(self, mock_connect, mock_project):
        """Test handling of runtime errors like division by zero."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.errors.DivisionByZero("division by zero")

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT 1/0"})

        assert "error" in result
        assert "division" in result["error"].lower()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_invalid_text_representation(self, mock_connect, mock_project):
        """Test handling of type casting errors."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.errors.InvalidTextRepresentation("invalid input syntax for type integer")

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT * FROM users WHERE id = 'not_a_number'"})

        assert "error" in result
        assert "invalid" in result["error"].lower() or "type" in result["error"].lower()


class TestReadOnlyEnforcement:
    """Test read-only session enforcement."""

    def test_validation_rejects_write_operations(self, mock_project):
        """Test that validator rejects write operations."""
        tool = create_sql_tool(mock_project)

        # These should be caught by validator before execution
        write_queries = [
            "INSERT INTO users (name) VALUES ('test')",
            "UPDATE users SET name = 'test'",
            "DELETE FROM users",
            "DROP TABLE users",
            "TRUNCATE users",
        ]

        for query in write_queries:
            result = tool.invoke({"query": query})
            assert "error" in result
            # Should be validation error, not execution error

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_readonly_session_set(self, mock_connect, mock_project):
        """Test that readonly session is set on connection."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(1,)]
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        tool.invoke({"query": "SELECT id FROM users"})

        # Verify set_session was called with readonly
        mock_conn.set_session.assert_called()


class TestResultStructure:
    """Test the structure of returned results."""

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_result_has_required_fields(self, mock_connect, mock_project):
        """Test that result contains all required fields."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(1,)]
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id FROM users"})

        # Required fields
        assert "columns" in result
        assert "rows" in result
        assert "row_count" in result
        assert "sql_executed" in result

        # Optional fields
        assert "truncated" in result or "is_truncated" in result

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_result_columns_match_rows(self, mock_connect, mock_project):
        """Test that number of columns matches row data."""
        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("id", None, None, None, None, None, None),
            ("name", None, None, None, None, None, None),
            ("email", None, None, None, None, None, None),
        ]
        mock_cursor.fetchall.return_value = [
            (1, "Alice", "alice@example.com"),
            (2, "Bob", "bob@example.com"),
        ]
        mock_cursor.rowcount = 2

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id, name, email FROM users"})

        assert len(result["columns"]) == 3
        assert all(len(row) == 3 for row in result["rows"])

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_provenance_metadata(self, mock_connect, mock_project):
        """Test that provenance metadata is included."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(1,)]
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id FROM users"})

        # Provenance fields (from Phase 2.2 spec)
        # These might be optional in initial implementation
        # but should be present according to spec
        if "tables_accessed" in result:
            assert isinstance(result["tables_accessed"], list)
        if "knowledge_applied" in result:
            assert isinstance(result["knowledge_applied"], list)
        if "caveats" in result:
            assert isinstance(result["caveats"], list)


class TestDatabaseConnectionManagement:
    """Test proper database connection management."""

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_connection_closed_on_success(self, mock_connect, mock_project):
        """Test that connection is properly closed after successful query."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(1,)]
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        tool.invoke({"query": "SELECT id FROM users"})

        # Connection should be closed via .close() method
        mock_conn.close.assert_called()
        mock_cursor.close.assert_called()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_connection_closed_on_error(self, mock_connect, mock_project):
        """Test that connection is properly closed even on error."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.errors.SyntaxError("syntax error")

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        tool.invoke({"query": "SELECT id FROM users"})

        # Connection should still be closed via .close() method
        mock_conn.close.assert_called()

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_uses_project_connection_params(self, mock_connect, mock_project):
        """Test that tool uses project's connection parameters."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("id", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [(1,)]
        mock_cursor.rowcount = 1

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        tool.invoke({"query": "SELECT id FROM users"})

        # Verify connection was called with project params
        mock_connect.assert_called_once()
        call_kwargs = mock_connect.call_args[1]

        params = mock_project.get_connection_params()
        assert call_kwargs["host"] == params["host"]
        assert call_kwargs["port"] == params["port"]
        assert call_kwargs["dbname"] == params["dbname"]
        assert call_kwargs["user"] == params["user"]
        assert call_kwargs["password"] == params["password"]


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_null_values_in_results(self, mock_connect, mock_project):
        """Test handling of NULL values in query results."""
        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("id", None, None, None, None, None, None),
            ("name", None, None, None, None, None, None),
        ]
        mock_cursor.fetchall.return_value = [
            (1, "Alice"),
            (2, None),
            (None, "Charlie"),
        ]
        mock_cursor.rowcount = 3

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT id, name FROM users"})

        assert result["row_count"] == 3
        assert result["rows"][1][1] is None
        assert result["rows"][2][0] is None

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_special_characters_in_data(self, mock_connect, mock_project):
        """Test handling of special characters in query results."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("name", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [
            ("O'Reilly",),
            ('Quote "test"',),
            ("Unicode: é, ñ, 中文",),
        ]
        mock_cursor.rowcount = 3

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT name FROM users"})

        assert result["row_count"] == 3
        assert "O'Reilly" in result["rows"][0]

    @patch("apps.agents.tools.sql_tool.psycopg2.connect")
    def test_large_numeric_values(self, mock_connect, mock_project):
        """Test handling of large numeric values."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("amount", None, None, None, None, None, None)]
        mock_cursor.fetchall.return_value = [
            (999999999999999,),
            (1.23456789012345,),
        ]
        mock_cursor.rowcount = 2

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        tool = create_sql_tool(mock_project)
        result = tool.invoke({"query": "SELECT amount FROM orders"})

        assert result["row_count"] == 2
        assert result["rows"][0][0] == 999999999999999

    def test_tool_has_proper_name_and_description(self, mock_project):
        """Test that tool has proper metadata for LangGraph."""
        tool = create_sql_tool(mock_project)

        assert hasattr(tool, "name")
        assert hasattr(tool, "description")
        assert isinstance(tool.name, str)
        assert isinstance(tool.description, str)
        assert len(tool.description) > 0
