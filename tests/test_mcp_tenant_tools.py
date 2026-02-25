"""
Tests for the tenant-based MCP server tools (list_tables, describe_table, get_metadata).

These tools query information_schema via execute_internal_query, bypassing
the SQL validator. Tests verify the full chain from tool handler through
to the parameterized query execution.
"""

from datetime import UTC
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


# ---------------------------------------------------------------------------
# get_schema_status tool
# ---------------------------------------------------------------------------

PATCH_TENANT_SCHEMA = "apps.projects.models.TenantSchema"
PATCH_MATERIALIZATION_RUN = "apps.projects.models.MaterializationRun"


class TestGetSchemaStatusTool:
    """Test the get_schema_status MCP tool."""

    async def test_returns_not_provisioned_when_no_schema(self, tenant_id):
        from mcp_server.server import get_schema_status

        with patch(PATCH_TENANT_SCHEMA) as mock_ts_cls:
            mock_qs = AsyncMock()
            mock_qs.afirst.return_value = None
            mock_ts_cls.objects.filter.return_value = mock_qs

            result = await get_schema_status(tenant_id)

        assert result["success"] is True
        assert result["data"]["exists"] is False
        assert result["data"]["state"] == "not_provisioned"
        assert result["data"]["tables"] == []
        assert result["data"]["last_materialized_at"] is None

    async def test_returns_active_schema_with_tables(self, tenant_id):
        from datetime import datetime

        from mcp_server.server import get_schema_status

        mock_schema = MagicMock()
        mock_schema.schema_name = "test_domain"
        mock_schema.state = "active"

        completed_at = datetime(2026, 2, 23, 10, 30, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"table": "cases", "rows_loaded": 15420}

        with (
            patch(PATCH_TENANT_SCHEMA) as mock_ts_cls,
            patch(PATCH_MATERIALIZATION_RUN) as mock_run_cls,
        ):
            mock_schema_qs = AsyncMock()
            mock_schema_qs.afirst.return_value = mock_schema
            mock_ts_cls.objects.filter.return_value = mock_schema_qs

            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value = mock_run_qs
            mock_run_qs.afirst = AsyncMock(return_value=mock_run)
            mock_run_cls.objects.filter.return_value = mock_run_qs

            result = await get_schema_status(tenant_id)

        assert result["success"] is True
        assert result["data"]["exists"] is True
        assert result["data"]["state"] == "active"
        assert result["data"]["last_materialized_at"] == "2026-02-23T10:30:00+00:00"
        assert result["data"]["tables"] == [{"name": "cases", "row_count": 15420}]
        assert result["schema"] == "test_domain"

    async def test_returns_tables_empty_when_no_completed_run(self, tenant_id):
        from mcp_server.server import get_schema_status

        mock_schema = MagicMock()
        mock_schema.schema_name = "test_domain"
        mock_schema.state = "active"

        with (
            patch(PATCH_TENANT_SCHEMA) as mock_ts_cls,
            patch(PATCH_MATERIALIZATION_RUN) as mock_run_cls,
        ):
            mock_schema_qs = AsyncMock()
            mock_schema_qs.afirst.return_value = mock_schema
            mock_ts_cls.objects.filter.return_value = mock_schema_qs

            mock_run_qs = MagicMock()
            mock_run_qs.order_by.return_value = mock_run_qs
            mock_run_qs.afirst = AsyncMock(return_value=None)
            mock_run_cls.objects.filter.return_value = mock_run_qs

            result = await get_schema_status(tenant_id)

        assert result["success"] is True
        assert result["data"]["exists"] is True
        assert result["data"]["tables"] == []
        assert result["data"]["last_materialized_at"] is None


# ---------------------------------------------------------------------------
# teardown_schema tool
# ---------------------------------------------------------------------------

PATCH_SCHEMA_MANAGER = "apps.projects.services.schema_manager.SchemaManager"


class TestTeardownSchemaTool:
    """Test the teardown_schema MCP tool."""

    async def test_requires_confirm_true(self, tenant_id):
        from mcp_server.server import teardown_schema

        result = await teardown_schema(tenant_id, confirm=False)

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "confirm=True" in result["error"]["message"]

    async def test_default_confirm_is_false(self, tenant_id):
        from mcp_server.server import teardown_schema

        result = await teardown_schema(tenant_id)

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    async def test_not_found_when_no_schema(self, tenant_id):
        from mcp_server.server import teardown_schema

        with patch(PATCH_TENANT_SCHEMA) as mock_ts_cls:
            mock_qs = MagicMock()
            mock_qs.exclude.return_value = mock_qs
            mock_qs.afirst = AsyncMock(return_value=None)
            mock_ts_cls.objects.filter.return_value = mock_qs

            result = await teardown_schema(tenant_id, confirm=True)

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND

    async def test_calls_schema_manager_teardown_on_confirm(self, tenant_id):
        from mcp_server.server import teardown_schema

        mock_schema = MagicMock()
        mock_schema.schema_name = "test_domain"

        with (
            patch(PATCH_TENANT_SCHEMA) as mock_ts_cls,
            patch(PATCH_SCHEMA_MANAGER) as mock_mgr_cls,
        ):
            mock_qs = MagicMock()
            mock_qs.exclude.return_value = mock_qs
            mock_qs.afirst = AsyncMock(return_value=mock_schema)
            mock_ts_cls.objects.filter.return_value = mock_qs

            mock_mgr = MagicMock()
            mock_mgr_cls.return_value = mock_mgr

            result = await teardown_schema(tenant_id, confirm=True)

        assert result["success"] is True
        assert result["data"]["schema_dropped"] == "test_domain"
        mock_mgr.teardown.assert_called_once_with(mock_schema)


class TestListPipelines:
    def test_returns_available_pipelines(self):
        import asyncio
        from unittest.mock import patch

        from mcp_server.pipeline_registry import PipelineConfig

        fake_pipelines = [
            PipelineConfig(
                name="commcare_sync",
                description="Sync case and form data from CommCare HQ",
                version="1.0",
                provider="commcare",
            )
        ]
        with patch("mcp_server.server.get_registry") as mock_reg:
            mock_reg.return_value.list.return_value = fake_pipelines
            from mcp_server.server import list_pipelines

            result = asyncio.run(list_pipelines())

        assert result["success"] is True
        assert len(result["data"]["pipelines"]) == 1
        assert result["data"]["pipelines"][0]["name"] == "commcare_sync"
        assert result["data"]["pipelines"][0]["provider"] == "commcare"


class TestGetMaterializationStatus:
    def test_returns_run_status(self):
        import asyncio
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = uuid.UUID(run_id)
        mock_run.pipeline = "commcare_sync"
        mock_run.state = "completed"
        mock_run.started_at.isoformat.return_value = "2026-02-24T10:00:00+00:00"
        mock_run.completed_at.isoformat.return_value = "2026-02-24T10:05:00+00:00"
        mock_run.result = {"sources": {"cases": {"rows": 100}}}
        mock_run.tenant_schema.tenant_membership.tenant_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            from mcp_server.server import get_materialization_status

            result = asyncio.run(get_materialization_status(run_id=run_id))

        assert result["success"] is True
        assert result["data"]["run_id"] == run_id
        assert result["data"]["state"] == "completed"

    def test_unknown_run_returns_not_found(self):
        import asyncio
        import uuid
        from unittest.mock import AsyncMock, patch

        from django.core.exceptions import ObjectDoesNotExist

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.DoesNotExist = ObjectDoesNotExist
            mock_cls.objects.select_related.return_value.aget = AsyncMock(
                side_effect=ObjectDoesNotExist
            )
            from mcp_server.server import get_materialization_status

            result = asyncio.run(get_materialization_status(run_id=str(uuid.uuid4())))

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"


class TestCancelMaterialization:
    def test_cancel_in_progress_run(self):
        import asyncio
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = uuid.UUID(run_id)
        mock_run.state = "loading"
        mock_run.result = {}
        mock_run.tenant_schema.tenant_membership.tenant_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            mock_cls.RunState.STARTED = "started"
            mock_cls.RunState.DISCOVERING = "discovering"
            mock_cls.RunState.LOADING = "loading"
            mock_cls.RunState.TRANSFORMING = "transforming"
            mock_cls.RunState.FAILED = "failed"
            from mcp_server.server import cancel_materialization

            result = asyncio.run(cancel_materialization(run_id=run_id))

        assert result["success"] is True
        assert result["data"]["cancelled"] is True
        assert result["data"]["run_id"] == run_id

    def test_cancel_completed_run_returns_error(self):
        import asyncio
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.state = "completed"
        mock_run.tenant_schema.tenant_membership.tenant_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            mock_cls.RunState.STARTED = "started"
            mock_cls.RunState.DISCOVERING = "discovering"
            mock_cls.RunState.LOADING = "loading"
            mock_cls.RunState.TRANSFORMING = "transforming"
            mock_cls.RunState.FAILED = "failed"
            from mcp_server.server import cancel_materialization

            result = asyncio.run(cancel_materialization(run_id=run_id))

        assert result["success"] is False
        assert "not in progress" in result["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Integration tests — require a real PostgreSQL connection
# ---------------------------------------------------------------------------


class TestExecuteSyncIntegration:
    """
    End-to-end tests for _execute_sync and _execute_sync_parameterized against
    a real PostgreSQL server.  These catch driver-level regressions (e.g. SET
    statement_timeout failing with psycopg3's server-side parameters) that
    mock-based tests can't detect.

    Skipped automatically when DATABASE_URL is not set.
    """

    @pytest.fixture(autouse=True)
    def real_db(self):
        import os
        from urllib.parse import urlparse

        import psycopg

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("No DATABASE_URL for integration test")

        parsed = urlparse(db_url)
        self.connection_params = {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/") or "scout",
            "user": parsed.username or "",
            "password": parsed.password or "",
        }
        self.schema = "test_query_exec"

        conn = psycopg.connect(**self.connection_params, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS "{self.schema}".items (
                        id SERIAL PRIMARY KEY,
                        name TEXT,
                        value INTEGER
                    )
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO "{self.schema}".items (name, value)
                    VALUES ('alpha', 1), ('beta', 2), ('gamma', 3)
                    """
                )
        finally:
            conn.close()

        yield

        conn = psycopg.connect(**self.connection_params, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            conn.close()

    def _ctx(self):
        from mcp_server.context import QueryContext

        return QueryContext(
            tenant_id="test-integration",
            schema_name=self.schema,
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            connection_params=self.connection_params,
        )

    def test_returns_rows(self):
        from mcp_server.services.query import _execute_sync

        result = _execute_sync(self._ctx(), "SELECT name, value FROM items ORDER BY value", 30)

        assert result["columns"] == ["name", "value"]
        assert result["rows"] == [["alpha", 1], ["beta", 2], ["gamma", 3]]
        assert result["row_count"] == 3

    def test_statement_timeout_does_not_use_server_side_param(self):
        """Regression: SET statement_timeout TO $1 raises SyntaxError in psycopg3."""
        from mcp_server.services.query import _execute_sync

        # Would raise psycopg.errors.SyntaxError before the fix
        result = _execute_sync(self._ctx(), "SELECT 1 AS n", 30)
        assert result["row_count"] == 1

    def test_empty_result(self):
        from mcp_server.services.query import _execute_sync

        result = _execute_sync(self._ctx(), "SELECT name FROM items WHERE value > 9999", 30)

        assert result["columns"] == ["name"]
        assert result["rows"] == []
        assert result["row_count"] == 0

    def test_parameterized_filters_rows(self):
        from mcp_server.services.query import _execute_sync_parameterized

        result = _execute_sync_parameterized(
            self._ctx(),
            "SELECT name, value FROM items WHERE value > %s ORDER BY value",
            (1,),
            30,
        )

        assert result["columns"] == ["name", "value"]
        assert result["rows"] == [["beta", 2], ["gamma", 3]]
        assert result["row_count"] == 2

    def test_search_path_is_applied(self):
        """Unqualified table name resolves because search_path is set to the schema."""
        from mcp_server.services.query import _execute_sync

        # No schema qualifier — relies on SET search_path TO working correctly
        result = _execute_sync(self._ctx(), "SELECT count(*) AS n FROM items", 30)

        assert result["row_count"] == 1
        assert result["rows"][0][0] == 3
