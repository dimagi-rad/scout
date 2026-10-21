"""
Integration tests for the MCP server tools.

Tests the full tool handler → service → response envelope chain.
Database access is mocked at the Django ORM / psycopg boundary.
"""

import logging
from unittest.mock import AsyncMock, patch

import psycopg
import psycopg.errors
import pytest

from mcp_server import envelope, server
from mcp_server.context import QueryContext
from mcp_server.envelope import (
    AUTH_TOKEN_EXPIRED,
    CONNECTION_ERROR,
    INTERNAL_ERROR,
    NOT_FOUND,
    QUERY_TIMEOUT,
    VALIDATION_ERROR,
    Timer,
    error_response,
    scrub_extra_fields,
    success_response,
)
from mcp_server.loaders.commcare_cases import CommCareAuthError, CommCareCaseLoader
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
    async def test_malformed_match_call_returns_envelope(self, project_context):
        result = await execute_query(project_context, "SELECT match_against(a, b) FROM t")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exception", [TypeError("bad AST"), ValueError("bad generation")])
    async def test_generation_failure_returns_envelope(self, project_context, exception):
        with patch("mcp_server.services.query.SQLValidator.inject_limit", side_effect=exception):
            result = await execute_query(project_context, "SELECT 1")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "supported PostgreSQL" in result["error"]["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_type",
        [
            psycopg.errors.GroupingError,
            psycopg.errors.AmbiguousColumn,
            psycopg.errors.WrongObjectType,
            psycopg.errors.InvalidColumnReference,
            psycopg.errors.CannotCoerce,
            psycopg.errors.DuplicateAlias,
            psycopg.errors.AmbiguousFunction,
            psycopg.errors.InvalidTextRepresentation,
            psycopg.errors.DivisionByZero,
            psycopg.errors.CardinalityViolation,
            psycopg.errors.FeatureNotSupported,
            psycopg.errors.SyntaxError,
            psycopg.errors.UndefinedFunction,
            psycopg.errors.UndefinedColumn,
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedObject,
            psycopg.errors.DatatypeMismatch,
            psycopg.errors.GroupingError,
        ],
    )
    @patch(POOLED_EXEC)
    async def test_sqlstate_query_error_is_actionable(
        self, mock_exec, project_context, error_type, caplog
    ):
        mock_exec.side_effect = error_type("invalid query detail")
        with caplog.at_level(logging.WARNING, logger="mcp_server.services.query"):
            result = await execute_query(project_context, "SELECT 1")
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "invalid query detail" in result["error"]["message"]
        assert "Reformulate" in result["error"]["message"]
        # The agent retries its own bad SQL; it must not reach Sentry, which takes ERROR.
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    @pytest.mark.asyncio
    async def test_unexpected_validation_failure_returns_envelope(self, project_context):
        with patch(
            "mcp_server.services.query.SQLValidator.validate", side_effect=TypeError("bad AST")
        ):
            result = await execute_query(project_context, "SELECT 1")
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            psycopg.errors.ConnectionFailure("connection lost"),
            psycopg.errors.InsufficientPrivilege("denied"),
            psycopg.errors.InvalidPassword("password authentication failed"),
            psycopg.OperationalError("does not exist is not a SQLSTATE"),
        ],
    )
    @patch(POOLED_EXEC)
    async def test_infrastructure_errors_stay_connection_errors(
        self, mock_exec, project_context, error, caplog
    ):
        mock_exec.side_effect = error
        with caplog.at_level(logging.WARNING, logger="mcp_server.services.query"):
            result = await execute_query(project_context, "SELECT 1")
        assert result["error"]["code"] == CONNECTION_ERROR
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

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
    async def test_a_row_past_the_cap_reports_truncation_and_is_dropped(
        self, mock_exec, project_context
    ):
        mock_exec.return_value = {
            "columns": ["id"],
            "rows": [[i] for i in range(501)],
            "row_count": 501,
        }
        result = await execute_query(project_context, "SELECT id FROM users")

        assert "LIMIT 501" in mock_exec.call_args.args[1].upper()
        assert "LIMIT 500" in result["sql_executed"].upper()
        assert result["truncated"] is True
        assert result["row_count"] == 500
        assert result["rows"][-1] == [499]
        assert result["row_limit"] == 500

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_a_result_of_exactly_the_cap_is_complete(self, mock_exec, project_context):
        mock_exec.return_value = {
            "columns": ["id"],
            "rows": [[i] for i in range(500)],
            "row_count": 500,
        }
        result = await execute_query(project_context, "SELECT id FROM users")
        assert result["truncated"] is False
        assert result["row_count"] == 500

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_limit_above_cap_is_lowered_and_truncates_only_when_rows_remain(
        self, mock_exec, project_context
    ):
        mock_exec.return_value = {"columns": ["id"], "rows": [[1]] * 300, "row_count": 300}
        result = await execute_query(project_context, "SELECT id FROM users LIMIT 5000")
        assert "LIMIT 500" in result["sql_executed"].upper()
        assert result["truncated"] is False

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_a_limit_below_the_cap_runs_unprobed(self, mock_exec, project_context):
        mock_exec.return_value = {"columns": ["id"], "rows": [[1]] * 499, "row_count": 499}
        result = await execute_query(project_context, "SELECT id FROM users LIMIT 499")
        assert "LIMIT 499" in mock_exec.call_args.args[1].upper()
        assert result["truncated"] is False

    @pytest.mark.asyncio
    @patch(POOLED_EXEC)
    async def test_a_limit_of_exactly_the_cap_is_probed(self, mock_exec, project_context):
        mock_exec.return_value = {"columns": ["id"], "rows": [[1]] * 501, "row_count": 501}
        result = await execute_query(project_context, "SELECT id FROM users LIMIT 500")
        assert "LIMIT 501" in mock_exec.call_args.args[1].upper()
        assert result["truncated"] is True
        assert result["row_count"] == 500

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
    async def test_timeout_error(self, mock_exec, project_context, caplog):
        mock_exec.side_effect = psycopg.errors.QueryCanceled()
        with caplog.at_level(logging.WARNING, logger="mcp_server.services.query"):
            result = await execute_query(project_context, "SELECT * FROM users")
        assert result["success"] is False
        assert result["error"]["code"] == QUERY_TIMEOUT
        # The agent narrows a too-heavy query and retries (SCOUT-DJANGO-44); not a Sentry event.
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

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
# code paths.


# --- Audit log scrubbing ---


class TestAuditLogScrubbing:
    """scrub_extra_fields removes any keys listed in _SCRUB_KEYS from audit
    extra_fields. The set is currently empty (the oauth_tokens transport was
    removed, arch #253 / 01#0); the hook stays for future sensitive fields."""

    def test_scrub_removes_listed_keys(self):

        with patch.object(envelope, "_SCRUB_KEYS", frozenset({"secret"})):
            scrubbed = scrub_extra_fields({"sql": "SELECT 1", "secret": "x"})
        assert "secret" not in scrubbed
        assert scrubbed["sql"] == "SELECT 1"

    def test_scrub_noop_when_nothing_to_scrub(self):

        extra = {"measures": ["visits.count"]}
        assert scrub_extra_fields(extra) == {"measures": ["visits.count"]}


class TestAuthTokenExpiredCode:
    """Test AUTH_TOKEN_EXPIRED error code exists."""

    def test_code_defined(self):

        assert AUTH_TOKEN_EXPIRED == "AUTH_TOKEN_EXPIRED"


class TestCommCareCaseLoaderAuth:
    def test_uses_bearer_header_for_oauth(self, requests_mock):

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


@pytest.mark.django_db(transaction=True)
class TestRowLimitWarning:
    """A result cut off at the row cap tells the agent so (#969)."""

    @pytest.mark.asyncio
    async def test_a_cut_off_sql_result_is_flagged_incomplete(self, project_context):
        cut = {
            "columns": ["id"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": True,
            "row_limit": 500,
            "sql_executed": "SELECT id FROM users LIMIT 500",
            "tables_accessed": ["users"],
        }
        with (
            patch.object(server, "load_workspace_context", AsyncMock(return_value=project_context)),
            patch.object(server, "execute_query", AsyncMock(return_value=cut)),
        ):
            result = await server.query("SELECT id FROM users", workspace_id="ws")

        assert result["data"]["row_limit"] == project_context.max_rows_per_query
        assert result["data"]["truncated"] is True
        assert "Only the first 500 rows" in result["warnings"][0]
        assert "do not present these rows as complete" in result["warnings"][0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row_count", "expected"),
        [(500, "Only the first 500 rows"), (20, "requested limit of 20 rows")],
    )
    async def test_semantic_results_tell_the_cap_from_the_agents_own_limit(
        self, row_count, expected
    ):
        result = {"columns": ["n"], "rows": [[1]] * row_count, "row_count": row_count}
        with (
            patch.object(server, "_resolve_accessible_workspace", AsyncMock()),
            patch.object(
                server,
                "run_semantic_query",
                AsyncMock(return_value={**result, "truncated": True}),
            ),
        ):
            envelope = await server.semantic_query(measures=["visits.count"], workspace_id="ws")

        assert envelope["data"]["row_limit"] == 500
        assert expected in envelope["warnings"][0]
