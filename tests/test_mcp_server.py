"""
Integration tests for the MCP server tools.

Tests the full tool handler → service → response envelope chain.
Database access is mocked at the Django ORM / psycopg2 boundary.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from mcp_server.context import ProjectContext, load_project_context
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
def project_id():
    return str(uuid.uuid4())


@pytest.fixture
def project_context(project_id):
    """A ProjectContext that doesn't require DB access."""
    return ProjectContext(
        project_id=project_id,
        project_name="Test Project",
        db_schema="public",
        allowed_tables=[],
        excluded_tables=[],
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        readonly_role="readonly",
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


# --- ProjectContext loading tests (mocked) ---


class TestLoadProjectContext:

    @pytest.mark.asyncio
    async def test_invalid_project_id_raises(self):
        """Loading a non-existent project raises ValueError."""
        fake_id = str(uuid.uuid4())
        mock_qs = AsyncMock()

        with patch("apps.projects.models.Project") as MockProject:
            MockProject.DoesNotExist = type("DoesNotExist", (Exception,), {})
            MockProject.objects.select_related.return_value = mock_qs
            mock_qs.aget.side_effect = MockProject.DoesNotExist()

            with pytest.raises(ValueError, match="not found or not active"):
                await load_project_context(fake_id)

    @pytest.mark.asyncio
    async def test_inactive_connection_raises(self):
        """Loading a project with inactive DB connection raises ValueError."""
        mock_project = type("Project", (), {
            "id": uuid.uuid4(),
            "name": "Test",
            "database_connection": type("Conn", (), {"is_active": False})(),
        })()

        mock_qs = AsyncMock()
        mock_qs.aget.return_value = mock_project

        with patch("apps.projects.models.Project") as MockProject:
            MockProject.DoesNotExist = type("DoesNotExist", (Exception,), {})
            MockProject.objects.select_related.return_value = mock_qs

            with pytest.raises(ValueError, match="not active"):
                await load_project_context(str(mock_project.id))


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
        result = await execute_query(
            project_context, "SELECT pg_read_file('/etc/passwd')"
        )
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "not allowed" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_excluded_table_rejected(self):
        ctx = ProjectContext(
            project_id=str(uuid.uuid4()),
            project_name="Test",
            db_schema="public",
            allowed_tables=[],
            excluded_tables=["secrets"],
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            readonly_role="",
            connection_params={},
        )
        result = await execute_query(ctx, "SELECT * FROM secrets")
        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

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


class TestToolHandlers:
    """Test the MCP tool handlers end-to-end (with mocked services)."""

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_list_tables_success(self, mock_load, project_context):
        from mcp_server.server import list_tables

        mock_load.return_value = project_context

        with patch("mcp_server.services.metadata.list_tables", new_callable=AsyncMock) as mock_lt:
            mock_lt.return_value = [
                {"name": "users", "type": "table", "row_count": 100, "column_count": 5},
            ]
            result = await list_tables(project_context.project_id)

        assert result["success"] is True
        assert len(result["data"]["tables"]) == 1
        assert result["data"]["tables"][0]["name"] == "users"
        assert result["project_id"] == project_context.project_id
        assert result["schema"] == "public"
        assert "timing_ms" in result

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_list_tables_invalid_project(self, mock_load):
        from mcp_server.server import list_tables

        mock_load.side_effect = ValueError("Project 'bad-id' not found or not active")
        result = await list_tables("bad-id")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_describe_table_success(self, mock_load, project_context):
        from mcp_server.server import describe_table

        mock_load.return_value = project_context

        table_data = {
            "name": "users",
            "columns": [{"name": "id", "type": "integer"}],
            "primary_key": ["id"],
            "foreign_keys": [],
            "indexes": [],
        }
        with patch(
            "mcp_server.services.metadata.describe_table", new_callable=AsyncMock
        ) as mock_dt:
            mock_dt.return_value = table_data
            result = await describe_table(project_context.project_id, "users")

        assert result["success"] is True
        assert result["data"]["name"] == "users"

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_describe_table_not_found(self, mock_load, project_context):
        from mcp_server.server import describe_table

        mock_load.return_value = project_context

        with patch(
            "mcp_server.services.metadata.describe_table", new_callable=AsyncMock
        ) as mock_dt, patch(
            "mcp_server.services.metadata.suggest_tables", new_callable=AsyncMock
        ) as mock_suggest:
            mock_dt.return_value = None
            mock_suggest.return_value = ["users", "user_roles"]
            result = await describe_table(project_context.project_id, "usr")

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND
        assert "Did you mean" in result["error"]["detail"]

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_get_metadata_success(self, mock_load, project_context):
        from mcp_server.server import get_metadata

        mock_load.return_value = project_context

        snapshot = {
            "schema": "public",
            "table_count": 2,
            "tables": {"users": {}, "orders": {}},
            "enums": {},
        }
        with patch(
            "mcp_server.services.metadata.get_metadata", new_callable=AsyncMock
        ) as mock_gm:
            mock_gm.return_value = snapshot
            result = await get_metadata(project_context.project_id)

        assert result["success"] is True
        assert result["data"]["table_count"] == 2

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_query_success(self, mock_load, project_context):
        from mcp_server.server import query

        mock_load.return_value = project_context

        with patch("mcp_server.services.query.execute_query", new_callable=AsyncMock) as mock_eq:
            mock_eq.return_value = {
                "columns": ["id"],
                "rows": [[1], [2]],
                "row_count": 2,
                "truncated": False,
                "sql_executed": "SELECT id FROM users LIMIT 500",
                "tables_accessed": ["users"],
            }
            result = await query(project_context.project_id, "SELECT id FROM users")

        assert result["success"] is True
        assert result["data"]["row_count"] == 2
        assert result["data"]["columns"] == ["id"]

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_query_truncation_warning(self, mock_load, project_context):
        from mcp_server.server import query

        mock_load.return_value = project_context

        with patch("mcp_server.services.query.execute_query", new_callable=AsyncMock) as mock_eq:
            mock_eq.return_value = {
                "columns": ["id"],
                "rows": [[i] for i in range(500)],
                "row_count": 500,
                "truncated": True,
                "sql_executed": "SELECT id FROM users LIMIT 500",
                "tables_accessed": ["users"],
            }
            result = await query(project_context.project_id, "SELECT id FROM users")

        assert result["success"] is True
        assert "warnings" in result
        assert any("truncated" in w.lower() for w in result["warnings"])

    @pytest.mark.asyncio
    @patch("mcp_server.server.load_project_context")
    async def test_query_validation_error_passthrough(self, mock_load, project_context):
        from mcp_server.server import query

        mock_load.return_value = project_context

        with patch("mcp_server.services.query.execute_query", new_callable=AsyncMock) as mock_eq:
            mock_eq.return_value = error_response(VALIDATION_ERROR, "Only SELECT allowed")
            result = await query(project_context.project_id, "DROP TABLE users")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
