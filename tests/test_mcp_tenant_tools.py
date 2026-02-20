"""
Tests for the tenant-based MCP server tools (list_tables, describe_table, get_metadata).

These tools query information_schema via execute_internal_query, bypassing
the SQL validator. Tests verify the full chain from tool handler through
to the parameterized query execution.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings

from mcp_server.context import QueryContext
from mcp_server.envelope import NOT_FOUND, VALIDATION_ERROR

# All async tests in this module use pytest-asyncio
pytestmark = pytest.mark.asyncio(loop_scope="function")

# Patch target: the helpers do `from mcp_server.services.query import execute_internal_query`
# inside the function body, so we must patch on the source module.
PATCH_INTERNAL_QUERY = "mcp_server.services.query.execute_internal_query"
PATCH_TENANT_CONTEXT = "mcp_server.server.load_tenant_context"


@pytest.fixture
def tenant_id():
    return "test-domain"


@pytest.fixture
def schema_name():
    return "test_domain"


@pytest.fixture
def tenant_context(tenant_id, schema_name):
    """A QueryContext representing a tenant (as returned by load_tenant_context)."""
    return QueryContext(
        tenant_id=tenant_id,
        schema_name=schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params={
            "host": "localhost",
            "port": 5432,
            "dbname": "scout",
            "user": "testuser",
            "password": "testpass",
            "options": f"-c search_path={schema_name},public -c statement_timeout=30000",
        },
    )


# ---------------------------------------------------------------------------
# execute_internal_query
# ---------------------------------------------------------------------------


class TestExecuteInternalQuery:
    """Test that execute_internal_query bypasses validation and passes params."""

    @patch("mcp_server.services.query._execute_sync_parameterized")
    async def test_passes_sql_and_params(self, mock_exec, tenant_context):
        from mcp_server.services.query import execute_internal_query

        mock_exec.return_value = {
            "columns": ["table_name"],
            "rows": [["cases"]],
            "row_count": 1,
        }

        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
        params = ("test_domain",)
        result = await execute_internal_query(tenant_context, sql, params)

        mock_exec.assert_called_once_with(tenant_context, sql, params, 30)
        assert result["row_count"] == 1
        assert result["rows"] == [["cases"]]

    @patch("mcp_server.services.query._execute_sync_parameterized")
    async def test_does_not_validate_sql(self, mock_exec, tenant_context):
        """Internal queries should NOT go through the SQL validator."""
        from mcp_server.services.query import execute_internal_query

        mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}

        # This SQL references information_schema — the validator blocked it before.
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
        result = await execute_internal_query(tenant_context, sql, ("test_domain",))

        assert "error" not in result
        mock_exec.assert_called_once()

    @patch("mcp_server.services.query._execute_sync_parameterized")
    async def test_does_not_inject_limit(self, mock_exec, tenant_context):
        """Internal queries should NOT have LIMIT injected."""
        from mcp_server.services.query import execute_internal_query

        mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}

        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
        await execute_internal_query(tenant_context, sql, ("test_domain",))

        # The SQL passed to _execute_sync_parameterized should be unchanged
        called_sql = mock_exec.call_args[0][1]
        assert "LIMIT" not in called_sql.upper()

    @patch("mcp_server.services.query._execute_sync_parameterized")
    async def test_returns_error_envelope_on_exception(self, mock_exec, tenant_context):
        from mcp_server.services.query import execute_internal_query

        mock_exec.side_effect = RuntimeError("connection failed")
        result = await execute_internal_query(tenant_context, "SELECT 1", ())

        assert result["success"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# _execute_sync_parameterized
# ---------------------------------------------------------------------------


class TestExecuteSyncParameterized:
    """Test the low-level sync execution function."""

    def test_sets_search_path_and_executes_with_params(self, tenant_context):
        from mcp_server.services.query import _execute_sync_parameterized

        mock_cursor = MagicMock()
        mock_cursor.description = [("table_name",), ("table_type",)]
        mock_cursor.fetchall.return_value = [("cases", "BASE TABLE")]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("mcp_server.services.query._get_connection", return_value=mock_conn):
            result = _execute_sync_parameterized(
                tenant_context,
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = %s",
                ("test_domain",),
                30,
            )

        # Verify all three execute calls: SET search_path, SET timeout, actual query
        execute_calls = mock_cursor.execute.call_args_list
        assert len(execute_calls) == 3

        # Verify the actual query was called with params
        final_call = execute_calls[2]
        assert "information_schema.tables" in final_call[0][0]
        assert final_call[0][1] == ("test_domain",)

        assert result == {
            "columns": ["table_name", "table_type"],
            "rows": [["cases", "BASE TABLE"]],
            "row_count": 1,
        }

    def test_returns_empty_rows_when_no_data(self, tenant_context):
        from mcp_server.services.query import _execute_sync_parameterized

        mock_cursor = MagicMock()
        mock_cursor.description = [("table_name",), ("table_type",)]
        mock_cursor.fetchall.return_value = []

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("mcp_server.services.query._get_connection", return_value=mock_conn):
            result = _execute_sync_parameterized(
                tenant_context,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                ("nonexistent_schema",),
                30,
            )

        assert result["rows"] == []
        assert result["row_count"] == 0


# ---------------------------------------------------------------------------
# _tenant_list_tables
# ---------------------------------------------------------------------------


class TestTenantListTables:
    """Test the _tenant_list_tables helper."""

    async def test_queries_information_schema_with_correct_params(self, tenant_context):
        from mcp_server.server import _tenant_list_tables

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["table_name", "table_type"],
                "rows": [["cases", "BASE TABLE"], ["locations", "BASE TABLE"]],
                "row_count": 2,
            }

            await _tenant_list_tables(tenant_context)

        # Verify the SQL uses parameterized query with the schema name
        mock_query.assert_called_once()
        called_args = mock_query.call_args
        called_ctx = called_args[0][0]
        called_sql = called_args[0][1]
        called_params = called_args[0][2]

        assert called_ctx is tenant_context
        assert "information_schema.tables" in called_sql
        assert "table_schema = %s" in called_sql
        assert called_params == ("test_domain",)

    async def test_returns_formatted_table_list(self, tenant_context):
        from mcp_server.server import _tenant_list_tables

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["table_name", "table_type"],
                "rows": [["cases", "BASE TABLE"], ["my_view", "VIEW"]],
                "row_count": 2,
            }

            tables = await _tenant_list_tables(tenant_context)

        assert len(tables) == 2
        assert tables[0] == {"name": "cases", "type": "table", "description": ""}
        assert tables[1] == {"name": "my_view", "type": "view", "description": ""}

    async def test_raises_on_error(self, tenant_context):
        from mcp_server.server import _tenant_list_tables

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "success": False,
                "error": {"code": "CONNECTION_ERROR", "message": "fail"},
            }

            with pytest.raises(RuntimeError, match="fail"):
                await _tenant_list_tables(tenant_context)

    async def test_returns_empty_list_when_no_tables(self, tenant_context):
        from mcp_server.server import _tenant_list_tables

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["table_name", "table_type"],
                "rows": [],
                "row_count": 0,
            }

            tables = await _tenant_list_tables(tenant_context)

        assert tables == []

    async def test_uses_ctx_schema_name_not_hardcoded(self):
        """Ensure the schema name comes from context, not hardcoded."""
        from mcp_server.server import _tenant_list_tables

        custom_ctx = QueryContext(
            tenant_id="my-org",
            schema_name="my_custom_schema",
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            connection_params={},
        )

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["table_name", "table_type"],
                "rows": [],
                "row_count": 0,
            }

            await _tenant_list_tables(custom_ctx)

        called_params = mock_query.call_args[0][2]
        assert called_params == ("my_custom_schema",)


# ---------------------------------------------------------------------------
# _tenant_describe_table
# ---------------------------------------------------------------------------


class TestTenantDescribeTable:
    """Test the _tenant_describe_table helper."""

    async def test_queries_information_schema_with_correct_params(self, tenant_context):
        from mcp_server.server import _tenant_describe_table

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [
                    ["case_id", "text", "NO", None],
                    ["case_type", "text", "YES", None],
                ],
                "row_count": 2,
            }

            await _tenant_describe_table(tenant_context, "cases")

        mock_query.assert_called_once()
        called_sql = mock_query.call_args[0][1]
        called_params = mock_query.call_args[0][2]

        assert "information_schema.columns" in called_sql
        assert "table_schema = %s" in called_sql
        assert "table_name = %s" in called_sql
        assert called_params == ("test_domain", "cases")

    async def test_returns_formatted_columns(self, tenant_context):
        from mcp_server.server import _tenant_describe_table

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [
                    ["case_id", "text", "NO", None],
                    ["properties", "jsonb", "YES", "'{}'::jsonb"],
                ],
                "row_count": 2,
            }

            result = await _tenant_describe_table(tenant_context, "cases")

        assert result is not None
        assert result["name"] == "cases"
        assert len(result["columns"]) == 2
        assert result["columns"][0] == {
            "name": "case_id",
            "type": "text",
            "nullable": False,
            "default": None,
        }
        assert result["columns"][1] == {
            "name": "properties",
            "type": "jsonb",
            "nullable": True,
            "default": "'{}'::jsonb",
        }

    async def test_returns_none_when_table_not_found(self, tenant_context):
        from mcp_server.server import _tenant_describe_table

        with patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query:
            mock_query.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [],
                "row_count": 0,
            }

            result = await _tenant_describe_table(tenant_context, "nonexistent")

        assert result is None


# ---------------------------------------------------------------------------
# list_tables tool handler
# ---------------------------------------------------------------------------


class TestListTablesTool:
    """Test the list_tables MCP tool handler end-to-end."""

    async def test_success(self, tenant_id, tenant_context):
        from mcp_server.server import list_tables

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query,
        ):
            mock_ctx.return_value = tenant_context
            mock_query.return_value = {
                "columns": ["table_name", "table_type"],
                "rows": [["cases", "BASE TABLE"]],
                "row_count": 1,
            }

            result = await list_tables(tenant_id)

        assert result["success"] is True
        assert len(result["data"]["tables"]) == 1
        assert result["data"]["tables"][0]["name"] == "cases"
        assert result["tenant_id"] == tenant_id
        assert result["schema"] == "test_domain"

    async def test_invalid_tenant_returns_validation_error(self):
        from mcp_server.server import list_tables

        with patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema for tenant 'bad'")

            result = await list_tables("bad")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    async def test_empty_schema_returns_empty_tables(self, tenant_id, tenant_context):
        from mcp_server.server import list_tables

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query,
        ):
            mock_ctx.return_value = tenant_context
            mock_query.return_value = {
                "columns": ["table_name", "table_type"],
                "rows": [],
                "row_count": 0,
            }

            result = await list_tables(tenant_id)

        assert result["success"] is True
        assert result["data"]["tables"] == []


# ---------------------------------------------------------------------------
# describe_table tool handler
# ---------------------------------------------------------------------------


class TestDescribeTableTool:
    """Test the describe_table MCP tool handler end-to-end."""

    async def test_success(self, tenant_id, tenant_context):
        from mcp_server.server import describe_table

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query,
        ):
            mock_ctx.return_value = tenant_context
            mock_query.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["case_id", "text", "NO", None]],
                "row_count": 1,
            }

            result = await describe_table(tenant_id, "cases")

        assert result["success"] is True
        assert result["data"]["name"] == "cases"
        assert result["data"]["columns"][0]["name"] == "case_id"

    async def test_table_not_found(self, tenant_id, tenant_context):
        from mcp_server.server import describe_table

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query,
        ):
            mock_ctx.return_value = tenant_context
            mock_query.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [],
                "row_count": 0,
            }

            result = await describe_table(tenant_id, "nonexistent")

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND

    async def test_invalid_tenant_returns_validation_error(self):
        from mcp_server.server import describe_table

        with patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema")

            result = await describe_table("bad", "cases")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# get_metadata tool handler
# ---------------------------------------------------------------------------


class TestGetMetadataTool:
    """Test the get_metadata MCP tool handler."""

    async def test_combines_list_and_describe(self, tenant_id, tenant_context):
        from mcp_server.server import get_metadata

        # Two calls: first for list_tables, then for describe of each table
        query_results = [
            # _tenant_list_tables call
            {
                "columns": ["table_name", "table_type"],
                "rows": [["cases", "BASE TABLE"]],
                "row_count": 1,
            },
            # _tenant_describe_table("cases") call
            {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["case_id", "text", "NO", None]],
                "row_count": 1,
            },
        ]

        with (
            patch(PATCH_TENANT_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch(PATCH_INTERNAL_QUERY, new_callable=AsyncMock) as mock_query,
        ):
            mock_ctx.return_value = tenant_context
            mock_query.side_effect = query_results

            result = await get_metadata(tenant_id)

        assert result["success"] is True
        assert result["data"]["table_count"] == 1
        assert "cases" in result["data"]["tables"]


# ---------------------------------------------------------------------------
# load_tenant_context
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestLoadTenantContext:
    """Test that load_tenant_context builds the correct QueryContext."""

    async def test_schema_name_in_context(self, tenant_membership):
        """Verify the schema name from TenantSchema flows into QueryContext.schema_name."""
        from apps.projects.models import SchemaState, TenantSchema
        from mcp_server.context import load_tenant_context

        await TenantSchema.objects.acreate(
            tenant_membership=tenant_membership,
            schema_name="dimagi",
            state=SchemaState.ACTIVE,
        )

        with override_settings(MANAGED_DATABASE_URL="postgresql://user:pass@localhost:5432/scout"):
            ctx = await load_tenant_context("test-domain")

        assert ctx.schema_name == "dimagi"
        assert ctx.tenant_id == "test-domain"
        assert ctx.connection_params["host"] == "localhost"
        assert ctx.connection_params["dbname"] == "scout"
        assert "search_path=dimagi" in ctx.connection_params["options"]

    async def test_raises_when_no_active_schema(self, tenant_membership):
        from mcp_server.context import load_tenant_context

        with pytest.raises(ValueError, match="No active schema"):
            await load_tenant_context("dimagi")

    async def test_raises_when_no_managed_db_url(self, tenant_membership):
        from apps.projects.models import SchemaState, TenantSchema
        from mcp_server.context import load_tenant_context

        await TenantSchema.objects.acreate(
            tenant_membership=tenant_membership,
            schema_name="dimagi",
            state=SchemaState.ACTIVE,
        )

        with override_settings(MANAGED_DATABASE_URL=""):
            with pytest.raises(ValueError, match="MANAGED_DATABASE_URL"):
                await load_tenant_context("test-domain")


# ---------------------------------------------------------------------------
# _parse_db_url
# ---------------------------------------------------------------------------


class TestParseDbUrl:
    """Test the URL parser that builds connection params."""

    def test_full_url(self):
        from mcp_server.context import _parse_db_url

        params = _parse_db_url("postgresql://myuser:mypass@dbhost:5433/mydb", "tenant_schema")

        assert params["host"] == "dbhost"
        assert params["port"] == 5433
        assert params["dbname"] == "mydb"
        assert params["user"] == "myuser"
        assert params["password"] == "mypass"
        assert "search_path=tenant_schema,public" in params["options"]

    def test_defaults_for_missing_fields(self):
        from mcp_server.context import _parse_db_url

        params = _parse_db_url("postgresql://localhost/scout", "my_schema")

        assert params["host"] == "localhost"
        assert params["port"] == 5432
        assert params["dbname"] == "scout"
        assert params["user"] == ""
        assert params["password"] == ""

    def test_bare_dbname_fallback(self):
        """In dev, MANAGED_DATABASE_URL may be just a database name."""
        from mcp_server.context import _parse_db_url

        params = _parse_db_url("scout", "my_schema")

        # urlparse("scout") gives path="scout", no host/port
        assert params["host"] == "localhost"
        assert params["port"] == 5432
        assert params["dbname"] == "scout"
