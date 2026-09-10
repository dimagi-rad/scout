"""
Integration tests for the MCP server tools.

Tests the full tool handler → service → response envelope chain.
Database access is mocked at the Django ORM / psycopg boundary.
"""

from unittest.mock import patch

import psycopg
import psycopg.errors
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

POOLED_EXEC = "mcp_server.services.query._execute_async_parameterized"


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
    async def test_system_catalog_rejected(self, project_context):
        result = await execute_query(project_context, "SELECT nspname FROM pg_namespace")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.asyncio
    async def test_unterminated_quote_returns_envelope(self, project_context):
        """A tokenizer error must come back as an envelope the agent can read and
        correct, not as an unhandled exception out of the tool."""
        result = await execute_query(project_context, "SELECT * FROM notes WHERE a = 'O'Brien'")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "parse error" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
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
    @patch(POOLED_EXEC)
    async def test_runs_through_the_shared_pooled_executor(self, mock_exec, project_context):
        """Agent SQL must share the pooled, role-resetting executor (arch #253, 10#1)
        rather than opening its own connection."""
        mock_exec.return_value = {"columns": ["id"], "rows": [], "row_count": 0}
        await execute_query(project_context, "SELECT id FROM users")

        ctx, sql, params, timeout = mock_exec.call_args.args
        assert ctx is project_context
        assert "LIMIT" in sql.upper()
        assert params == ()
        assert timeout == project_context.max_query_timeout_seconds

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
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
    @patch(POOLED_EXEC)
    async def test_limit_above_cap_is_lowered_and_reported_truncated(
        self, mock_exec, project_context
    ):
        mock_exec.return_value = {"columns": ["id"], "rows": [], "row_count": 0}
        result = await execute_query(project_context, "SELECT id FROM users LIMIT 5000")
        assert "LIMIT 500" in result["sql_executed"].upper()
        assert result["truncated"] is True

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_limit_injected(self, mock_exec, project_context):
        mock_exec.return_value = {
            "columns": ["id"],
            "rows": [],
            "row_count": 0,
        }
        result = await execute_query(project_context, "SELECT id FROM users")
        assert "LIMIT" in result["sql_executed"].upper()

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_timeout_error(self, mock_exec, project_context):
        mock_exec.side_effect = psycopg.errors.QueryCanceled()
        result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == QUERY_TIMEOUT

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_connection_error(self, mock_exec, project_context):
        mock_exec.side_effect = psycopg.OperationalError("could not connect to server")
        result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == CONNECTION_ERROR

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_unexpected_error(self, mock_exec, project_context):
        mock_exec.side_effect = RuntimeError("boom")
        result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == INTERNAL_ERROR


# --- Server tool handler tests ---
# NOTE: Tool handler tests for list_tables, describe_table, and get_metadata
# have been moved to test_mcp_tenant_tools.py which tests the current tenant-based
# code paths (load_tenant_context + execute_internal_query).


# --- Audit log scrubbing ---


class TestAuditLogScrubbing:
    """scrub_extra_fields removes any keys listed in _SCRUB_KEYS from audit
    extra_fields. The set is currently empty (the oauth_tokens transport was
    removed, arch #253 / 01#0); the hook stays for future sensitive fields."""

    def test_scrub_removes_listed_keys(self):
        from mcp_server import envelope
        from mcp_server.envelope import scrub_extra_fields

        with patch.object(envelope, "_SCRUB_KEYS", frozenset({"secret"})):
            scrubbed = scrub_extra_fields({"sql": "SELECT 1", "secret": "x"})
        assert "secret" not in scrubbed
        assert scrubbed["sql"] == "SELECT 1"

    def test_scrub_noop_when_nothing_to_scrub(self):
        from mcp_server.envelope import scrub_extra_fields

        extra = {"measures": ["visits.count"]}
        assert scrub_extra_fields(extra) == {"measures": ["visits.count"]}


class TestAuthTokenExpiredCode:
    """Test AUTH_TOKEN_EXPIRED error code exists."""

    def test_code_defined(self):
        from mcp_server.envelope import AUTH_TOKEN_EXPIRED

        assert AUTH_TOKEN_EXPIRED == "AUTH_TOKEN_EXPIRED"


class TestCommCareCaseLoaderAuth:
    def test_uses_bearer_header_for_oauth(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader

        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            json={"cases": [], "next": None},
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "oauth", "value": "mytoken"},
        )
        loader.load()
        assert requests_mock.last_request.headers["Authorization"] == "Bearer mytoken"

    def test_uses_apikey_header_for_api_key(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader

        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            json={"cases": [], "next": None},
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "api_key", "value": "user@example.com:abc123"},
        )
        loader.load()
        assert (
            requests_mock.last_request.headers["Authorization"] == "ApiKey user@example.com:abc123"
        )

    def test_raises_auth_error_on_401(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareAuthError, CommCareCaseLoader

        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            status_code=401,
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "api_key", "value": "user:key"},
        )
        with pytest.raises(CommCareAuthError):
            loader.load()
