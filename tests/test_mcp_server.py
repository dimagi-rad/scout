"""
Integration tests for the MCP server tools.

Tests the full tool handler → service → response envelope chain.
Database access is mocked at the Django ORM / psycopg2 boundary.
"""

from unittest.mock import patch

import pytest

from mcp_server.context import QueryContext
from mcp_server.envelope import (
    CONNECTION_ERROR,
    INTERNAL_ERROR,
    NOT_FOUND,
    QUERY_TIMEOUT,
    VALIDATION_ERROR,
    Timer,
    error_response,
    success_response,
)
from mcp_server.services.query import execute_query

# --- Fixtures ---


@pytest.fixture
def project_context():
    """A QueryContext that doesn't require DB access."""
    return QueryContext(
        tenant_id="test-tenant",
        schema_name="public",
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params={
            "host": "localhost",
            "port": 5432,
            "dbname": "testdb",
            "user": "testuser",
            "password": "testpass",
        },
    )


# --- Envelope tests ---


class TestEnvelopeFormat:
    """Verify the response envelope structure."""

    def test_success_response_structure(self):
        envelope = success_response(
            {"tables": ["users"]},
            project_id="abc",
            schema="public",
            timing_ms=42,
        )
        assert envelope["success"] is True
        assert envelope["data"] == {"tables": ["users"]}
        assert envelope["project_id"] == "abc"
        assert envelope["schema"] == "public"
        assert envelope["timing_ms"] == 42
        assert "warnings" not in envelope

    def test_success_response_with_warnings(self):
        envelope = success_response(
            {"rows": []},
            project_id="abc",
            schema="public",
            warnings=["Results truncated to 500 rows"],
        )
        assert envelope["warnings"] == ["Results truncated to 500 rows"]

    def test_success_response_omits_none_timing(self):
        envelope = success_response(
            {"rows": []},
            project_id="abc",
            schema="public",
        )
        assert "timing_ms" not in envelope

    def test_error_response_structure(self):
        envelope = error_response(VALIDATION_ERROR, "Bad SQL")
        assert envelope["success"] is False
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        assert envelope["error"]["message"] == "Bad SQL"
        assert "detail" not in envelope["error"]

    def test_error_response_with_detail(self):
        envelope = error_response(
            NOT_FOUND,
            "Table 'foo' not found",
            detail="Did you mean: foobar, foo_bar",
        )
        assert envelope["error"]["detail"] == "Did you mean: foobar, foo_bar"

    def test_timer_returns_positive_ms(self):
        timer = Timer()
        assert timer.elapsed_ms >= 0


# --- Query service tests ---


class TestExecuteQuery:
    """Test the query service with mocked DB execution."""

    @pytest.mark.asyncio
    async def test_validation_error_returns_envelope(self, project_context):
        result = await execute_query(project_context, "DROP TABLE users")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.asyncio
    async def test_multiple_statements_rejected(self, project_context):
        result = await execute_query(project_context, "SELECT 1; SELECT 2")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "multiple" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_dangerous_function_rejected(self, project_context):
        result = await execute_query(project_context, "SELECT pg_read_file('/etc/passwd')")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "not allowed" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    @patch("mcp_server.services.query._execute_sync")
    async def test_successful_query(self, mock_exec, project_context):
        mock_exec.return_value = {
            "columns": ["id", "name"],
            "rows": [[1, "Alice"], [2, "Bob"]],
            "row_count": 2,
        }
        result = await execute_query(project_context, "SELECT id, name FROM users")

        assert "columns" in result
        assert result["columns"] == ["id", "name"]
        assert result["row_count"] == 2
        assert result["truncated"] is False
        assert "sql_executed" in result
        assert "tables_accessed" in result
        assert "users" in result["tables_accessed"]

    @pytest.mark.asyncio
    @patch("mcp_server.services.query._execute_sync")
    async def test_truncation_detected(self, mock_exec, project_context):
        """When row_count equals max_limit, truncated should be True."""
        mock_exec.return_value = {
            "columns": ["id"],
            "rows": [[i] for i in range(500)],
            "row_count": 500,
        }
        result = await execute_query(project_context, "SELECT id FROM users")
        assert result["truncated"] is True

    @pytest.mark.asyncio
    @patch("mcp_server.services.query._execute_sync")
    async def test_limit_injected(self, mock_exec, project_context):
        mock_exec.return_value = {
            "columns": ["id"],
            "rows": [],
            "row_count": 0,
        }
        result = await execute_query(project_context, "SELECT id FROM users")
        assert "LIMIT" in result["sql_executed"].upper()

    @pytest.mark.asyncio
    @patch("mcp_server.services.query._execute_sync")
    async def test_timeout_error(self, mock_exec, project_context):
        import psycopg2.errors

        mock_exec.side_effect = psycopg2.errors.QueryCanceled()
        result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == QUERY_TIMEOUT

    @pytest.mark.asyncio
    @patch("mcp_server.services.query._execute_sync")
    async def test_connection_error(self, mock_exec, project_context):
        import psycopg2

        mock_exec.side_effect = psycopg2.OperationalError("could not connect to server")
        result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == CONNECTION_ERROR

    @pytest.mark.asyncio
    @patch("mcp_server.services.query._execute_sync")
    async def test_unexpected_error(self, mock_exec, project_context):
        mock_exec.side_effect = RuntimeError("boom")
        result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == INTERNAL_ERROR


# --- Server tool handler tests ---
# NOTE: Tool handler tests for list_tables, describe_table, get_metadata, and query
# have been moved to test_mcp_tenant_tools.py which tests the current tenant-based
# code paths (load_tenant_context + execute_internal_query).


# --- Auth token extraction tests ---


class TestAuthTokenExtraction:
    """Test MCP auth token extraction from _meta field."""

    def test_extract_tokens_from_meta(self):
        from mcp_server.auth import extract_oauth_tokens

        meta = {"oauth_tokens": {"commcare": "tok_abc", "commcare_connect": "tok_xyz"}}
        assert extract_oauth_tokens(meta) == {"commcare": "tok_abc", "commcare_connect": "tok_xyz"}

    def test_extract_tokens_missing_meta(self):
        from mcp_server.auth import extract_oauth_tokens

        assert extract_oauth_tokens({}) == {}

    def test_extract_tokens_none_meta(self):
        from mcp_server.auth import extract_oauth_tokens

        assert extract_oauth_tokens(None) == {}


class TestAuditLogScrubbing:
    """Test that oauth_tokens are scrubbed from audit log extra_fields."""

    def test_scrub_removes_oauth_tokens(self):
        from mcp_server.envelope import scrub_extra_fields

        extra = {"sql": "SELECT 1", "oauth_tokens": {"commcare": "secret"}}
        scrubbed = scrub_extra_fields(extra)
        assert "oauth_tokens" not in scrubbed
        assert scrubbed["sql"] == "SELECT 1"

    def test_scrub_noop_when_no_tokens(self):
        from mcp_server.envelope import scrub_extra_fields

        extra = {"sql": "SELECT 1"}
        assert scrub_extra_fields(extra) == {"sql": "SELECT 1"}


class TestAuthTokenExpiredCode:
    """Test AUTH_TOKEN_EXPIRED error code exists."""

    def test_code_defined(self):
        from mcp_server.envelope import AUTH_TOKEN_EXPIRED

        assert AUTH_TOKEN_EXPIRED == "AUTH_TOKEN_EXPIRED"
