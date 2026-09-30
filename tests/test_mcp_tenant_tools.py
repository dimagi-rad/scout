"""
Tests for the tenant-based MCP server tools (list_tables, describe_table, get_metadata).

Tests verify the full chain from tool handler through to query execution.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.test import override_settings

from apps.chat.models import Thread, ThreadJob
from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.access import WorkspaceAccess
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from mcp_server.context import QueryContext, _parse_db_url, load_tenant_context
from mcp_server.envelope import NOT_FOUND, VALIDATION_ERROR
from mcp_server.pipeline_registry import PipelineConfig
from mcp_server.server import (
    cancel_materialization,
    describe_table,
    get_materialization_status,
    get_metadata,
    get_schema_status,
    list_datasets,
    list_pipelines,
    list_tables,
    list_workspaces,
    semantic_catalog,
    teardown_schema,
)
from mcp_server.services.pool import close_all_pools
from mcp_server.services.query import _execute_async_parameterized, execute_query
from tests.managed_query_fixture import managed_query_context
from tests.tenant_access import ausable_connection

# All async tests in this module use pytest-asyncio
pytestmark = pytest.mark.asyncio(loop_scope="function")

PATCH_WORKSPACE_CONTEXT = "mcp_server.server.load_workspace_context"
# Pipeline resolution moved into apps.workspaces.services.pipeline_resolver,
# so the tenant lookup is patched where it is now consumed.
PATCH_RESOLVER_TENANT = "apps.workspaces.services.pipeline_resolver.Tenant"


@pytest_asyncio.fixture(autouse=True)
async def _close_managed_db_pools():
    yield
    await close_all_pools()


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
# _execute_async_parameterized
# ---------------------------------------------------------------------------


def _make_async_conn(mock_cursor):
    """Build a mock that mimics psycopg.AsyncConnection for async with patterns."""
    mock_conn = MagicMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    return mock_conn


def _make_pool_for_conn(mock_conn):
    """Build a mock AsyncConnectionPool whose .connection() yields ``mock_conn``.

    Queries now acquire connections from the shared managed-DB pool (arch #253,
    10#1) rather than opening a fresh ``psycopg.AsyncConnection`` per call, so
    tests patch ``get_pool`` to return this fake pool.
    """
    pool = MagicMock()
    pool.connection.return_value = mock_conn  # mock_conn is its own async ctx mgr
    return AsyncMock(return_value=pool)


class TestExecuteAsyncParameterized:
    """Test the low-level async execution function."""

    @pytest.mark.asyncio
    async def test_sets_search_path_and_executes_with_params(self, tenant_context):

        mock_cursor = AsyncMock()
        mock_cursor.description = [("table_name",), ("table_type",)]
        mock_cursor.fetchall.return_value = [("cases", "BASE TABLE")]

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "mcp_server.services.query.get_pool",
            new=_make_pool_for_conn(mock_conn),
        ):
            result = await _execute_async_parameterized(
                tenant_context,
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = %s",
                ("test_domain",),
                30,
            )

        # SET ROLE, search_path, timeout, actual query, then reset role/session.
        execute_calls = mock_cursor.execute.call_args_list
        assert len(execute_calls) == 7
        assert "SET ROLE" in str(execute_calls[0][0][0])
        assert "RESET ROLE" in str(execute_calls[-2])
        assert "RESET ALL" in str(execute_calls[-1])

        # Verify the actual query was called with params
        final_call = execute_calls[4]
        assert "information_schema.tables" in final_call[0][0]
        assert final_call[0][1] == ("test_domain",)

        assert result == {
            "columns": ["table_name", "table_type"],
            "rows": [["cases", "BASE TABLE"]],
            "row_count": 1,
        }

    @pytest.mark.asyncio
    async def test_returns_empty_rows_when_no_data(self, tenant_context):

        mock_cursor = AsyncMock()
        mock_cursor.description = [("table_name",), ("table_type",)]
        mock_cursor.fetchall.return_value = []

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "mcp_server.services.query.get_pool",
            new=_make_pool_for_conn(mock_conn),
        ):
            result = await _execute_async_parameterized(
                tenant_context,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                ("nonexistent_schema",),
                30,
            )

        assert result["rows"] == []
        assert result["row_count"] == 0


# ---------------------------------------------------------------------------
# list_tables tool handler
# ---------------------------------------------------------------------------

PATCH_PIPELINE_LIST_TABLES = "mcp_server.server.pipeline_list_tables"


def _fake_sync_to_async(fn):
    """Test helper: makes sync_to_async a transparent pass-through."""

    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


# Tool handlers run inside tool_context, which now manages DB connections
# (close_old_connections) on every call (arch #253, 08#0), so every tool-invoking
# test touches the connection layer and needs the DB enabled.
@pytest.mark.django_db(transaction=True)
class TestListTablesTool:
    async def test_success_returns_enriched_tables(self, tenant_id, tenant_context):

        mock_ts = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline = "commcare_sync"
        mock_tables = [
            {
                "name": "cases",
                "type": "table",
                "description": "CommCare cases",
                "materialized_row_count": 100,
                "row_count_verified": False,
                "materialized_at": "2026-02-24T10:00:00Z",
            }
        ]

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.WorkspaceViewSchema") as mock_vs_cls,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.synced_runs") as mock_synced_runs,
            patch(PATCH_PIPELINE_LIST_TABLES, return_value=mock_tables),
        ):
            mock_ctx.return_value = tenant_context
            mock_vs_cls.objects.filter.return_value.aexists = AsyncMock(return_value=False)
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_synced_runs.return_value.filter.return_value.afirst = AsyncMock(
                return_value=mock_run
            )

            result = await list_tables(workspace_id="ws-test")

        assert result["success"] is True
        assert len(result["data"]["tables"]) == 1
        assert result["data"]["tables"][0]["materialized_row_count"] == 100
        assert result["data"]["tables"][0]["row_count_verified"] is False
        assert result["data"]["note"] is None

    async def test_empty_tables_when_no_completed_run(self, tenant_id, tenant_context):

        mock_ts = MagicMock()
        mock_tenant = MagicMock()
        mock_tenant.provider = "commcare"

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.WorkspaceViewSchema") as mock_vs_cls,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch(PATCH_RESOLVER_TENANT) as mock_tenant_cls,
            patch("mcp_server.server.synced_runs") as mock_synced_runs,
            patch(PATCH_PIPELINE_LIST_TABLES, return_value=[]),
        ):
            mock_ctx.return_value = tenant_context
            mock_vs_cls.objects.filter.return_value.aexists = AsyncMock(return_value=False)
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tenant_cls.objects.aget = AsyncMock(return_value=mock_tenant)
            mock_synced_runs.return_value.filter.return_value.afirst = AsyncMock(return_value=None)

            result = await list_tables(workspace_id="ws-test")

        assert result["success"] is True
        assert result["data"]["tables"] == []
        assert "run_materialization" in result["data"]["note"]

    async def test_invalid_tenant_returns_validation_error(self):

        with patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema for tenant 'bad'")

            result = await list_tables(workspace_id="bad")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    async def test_returns_empty_when_no_tenant_schema(self, tenant_id, tenant_context):

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.WorkspaceViewSchema") as mock_vs_cls,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
        ):
            mock_ctx.return_value = tenant_context
            mock_vs_cls.objects.filter.return_value.aexists = AsyncMock(return_value=False)
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=None)

            result = await list_tables(workspace_id="ws-test")

            assert result["success"] is True
            assert result["data"]["tables"] == []


# ---------------------------------------------------------------------------
# workspace/dataset discovery tools
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestWorkspaceAndDatasetDiscoveryTools:
    async def test_list_workspaces_returns_accessible_workspaces(self, workspace, user):

        result = await list_workspaces(
            user_id=str(user.id),
            workspace_id=str(workspace.id),
            limit=10,
            offset=0,
        )

        assert result["success"] is True
        assert result["data"]["total"] == 1
        assert result["data"]["has_more"] is False
        item = result["data"]["workspaces"][0]
        assert item["id"] == str(workspace.id)
        assert item["name"] == workspace.name
        assert item["role"] == WorkspaceRole.MANAGE
        assert item["is_active"] is True
        assert item["tenants"][0]["canonical_name"] == "Test Domain"

    async def test_list_workspaces_requires_user_id(self):

        result = await list_workspaces()

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    async def test_list_datasets_pages_across_accessible_workspaces(self, workspace, user):

        model = await SemanticModel.objects.acreate(
            workspace=workspace,
            name="Test semantic model",
        )
        dataset = await SemanticDataset.objects.acreate(
            workspace=workspace,
            semantic_model=model,
            name="raw_users",
            label="Raw Users",
            description="Worker roster",
            schema_name="tenant_schema",
            table_name="raw_users",
            row_count=10,
            metadata={"row_count_verified": False},
        )
        await SemanticField.objects.acreate(
            dataset=dataset,
            name="count",
            label="Count",
            field_type=SemanticField.FieldType.MEASURE,
            data_type="integer",
            expression="*",
            measure_type=SemanticField.MeasureType.COUNT,
            is_visible=True,
        )

        result = await list_datasets(
            user_id=str(user.id),
            limit=10,
            offset=0,
            include_fields=True,
        )

        assert result["success"] is True
        assert result["data"]["total"] == 1
        item = result["data"]["datasets"][0]
        assert item["workspace"]["id"] == str(workspace.id)
        assert item["workspace"]["role"] == WorkspaceRole.MANAGE
        assert item["name"] == "raw_users"
        assert item["row_count"] == 10
        assert item["row_count_verified"] is False
        assert item["fields"][0]["member"] == "raw_users.count"

    async def test_list_datasets_filters_inaccessible_requested_workspaces(
        self, workspace, user, other_user, tenant
    ):

        other_workspace = await Workspace.objects.acreate(
            name="Other workspace",
            created_by=other_user,
        )
        await WorkspaceTenant.objects.acreate(workspace=other_workspace, tenant=tenant)
        await WorkspaceMembership.objects.acreate(
            workspace=other_workspace,
            user=other_user,
            role=WorkspaceRole.MANAGE,
        )

        result = await list_datasets(
            user_id=str(user.id),
            workspace_ids=[str(other_workspace.id)],
            limit=10,
            offset=0,
        )

        assert result["success"] is True
        assert result["data"]["datasets"] == []
        assert result["data"]["inaccessible_workspace_ids"] == [str(other_workspace.id)]

    async def test_semantic_catalog_rejects_inaccessible_workspace(
        self, workspace, user, other_user, tenant
    ):

        other_workspace = await Workspace.objects.acreate(
            name="Other workspace",
            created_by=other_user,
        )
        await WorkspaceTenant.objects.acreate(workspace=other_workspace, tenant=tenant)
        await WorkspaceMembership.objects.acreate(
            workspace=other_workspace,
            user=other_user,
            role=WorkspaceRole.MANAGE,
        )

        result = await semantic_catalog(
            user_id=str(user.id),
            workspace_id=str(other_workspace.id),
        )

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND


# ---------------------------------------------------------------------------
# describe_table tool handler
# ---------------------------------------------------------------------------

PATCH_PIPELINE_DESCRIBE_TABLE = "mcp_server.server.pipeline_describe_table"


@pytest.mark.django_db(transaction=True)
class TestDescribeTableTool:
    async def test_success_returns_enriched_columns(self, tenant_id, tenant_context):

        mock_ts = MagicMock()
        mock_ts.tenant_membership = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline = "commcare_sync"
        mock_table = {
            "name": "cases",
            "description": "CommCare case records",
            "columns": [
                {
                    "name": "case_id",
                    "type": "text",
                    "nullable": False,
                    "default": None,
                    "description": "",
                },
                {
                    "name": "properties",
                    "type": "jsonb",
                    "nullable": True,
                    "default": None,
                    "description": "Contains case properties. Available case types: pregnancy",
                },
            ],
        }

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.aget_tenant_metadata", new_callable=AsyncMock) as mock_tm,
            patch("mcp_server.server.synced_runs") as mock_synced_runs,
            patch(PATCH_PIPELINE_DESCRIBE_TABLE, return_value=mock_table),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tm.return_value = MagicMock()
            mock_synced_runs.return_value.filter.return_value.afirst = AsyncMock(
                return_value=mock_run
            )

            result = await describe_table("cases", workspace_id="ws-test")

        assert result["success"] is True
        assert result["data"]["name"] == "cases"
        assert result["data"]["description"] == "CommCare case records"
        assert "properties" in [c["name"] for c in result["data"]["columns"]]

    async def test_table_not_found(self, tenant_id, tenant_context):

        mock_ts = MagicMock()
        mock_ts.tenant_membership = MagicMock()
        mock_tenant = MagicMock()
        mock_tenant.provider = "commcare"

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch(PATCH_RESOLVER_TENANT) as mock_tenant_cls,
            patch("mcp_server.server.aget_tenant_metadata", new_callable=AsyncMock) as mock_tm,
            patch("mcp_server.server.synced_runs") as mock_synced_runs,
            patch(PATCH_PIPELINE_DESCRIBE_TABLE, return_value=None),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tenant_cls.objects.aget = AsyncMock(return_value=mock_tenant)
            mock_tm.return_value = None
            mock_synced_runs.return_value.filter.return_value.afirst = AsyncMock(return_value=None)

            result = await describe_table("nonexistent", workspace_id="ws-test")

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND

    async def test_invalid_tenant_returns_validation_error(self):

        with patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema")
            result = await describe_table("cases", workspace_id="bad")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# get_metadata tool handler
# ---------------------------------------------------------------------------

PATCH_PIPELINE_GET_METADATA = "mcp_server.server.pipeline_get_metadata"


@pytest.mark.django_db(transaction=True)
class TestGetMetadataTool:
    async def test_returns_tables_and_relationships(self, tenant_id, tenant_context):

        mock_ts = MagicMock()
        mock_ts.tenant_membership = MagicMock()
        mock_run = MagicMock()
        mock_run.pipeline = "commcare_sync"
        mock_result = {
            "tables": {
                "cases": {
                    "name": "cases",
                    "description": "CommCare cases",
                    "columns": [
                        {
                            "name": "case_id",
                            "type": "text",
                            "nullable": False,
                            "default": None,
                            "description": "",
                        }
                    ],
                }
            },
            "relationships": [
                {
                    "from_table": "forms",
                    "from_column": "case_ids",
                    "to_table": "cases",
                    "to_column": "case_id",
                    "description": "",
                }
            ],
        }

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
            patch("mcp_server.server.aget_tenant_metadata", new_callable=AsyncMock) as mock_tm,
            patch("mcp_server.server.synced_runs") as mock_synced_runs,
            patch(PATCH_PIPELINE_GET_METADATA, return_value=mock_result),
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=mock_ts)
            mock_tm.return_value = MagicMock()
            mock_synced_runs.return_value.filter.return_value.afirst = AsyncMock(
                return_value=mock_run
            )

            result = await get_metadata(workspace_id="ws-test")

        assert result["success"] is True
        assert result["data"]["table_count"] == 1
        assert "cases" in result["data"]["tables"]
        assert len(result["data"]["relationships"]) == 1

    async def test_returns_empty_when_no_active_schema(self, tenant_id, tenant_context):

        with (
            patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx,
            patch("mcp_server.server.TenantSchema") as mock_ts_cls,
        ):
            mock_ctx.return_value = tenant_context
            mock_ts_cls.objects.filter.return_value.afirst = AsyncMock(return_value=None)

            result = await get_metadata(workspace_id="ws-test")

        assert result["success"] is True
        assert result["data"]["table_count"] == 0
        assert result["data"]["tables"] == {}
        assert result["data"]["relationships"] == []

    async def test_invalid_tenant_returns_validation_error(self):

        with patch(PATCH_WORKSPACE_CONTEXT, new_callable=AsyncMock) as mock_ctx:
            mock_ctx.side_effect = ValueError("No active schema")
            result = await get_metadata(workspace_id="bad")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# load_tenant_context
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestLoadTenantContext:
    """Test that load_tenant_context builds the correct QueryContext."""

    async def test_schema_name_in_context(self, tenant_membership):
        """Verify the schema name from TenantSchema flows into QueryContext.schema_name."""
        await TenantSchema.objects.acreate(
            tenant=tenant_membership.tenant,
            schema_name="dimagi",
            state=SchemaState.ACTIVE,
        )

        with override_settings(MANAGED_DATABASE_URL="postgresql://user:pass@localhost:5432/scout"):
            ctx = await load_tenant_context("test-domain", "commcare")

        assert ctx.schema_name == "dimagi"
        assert ctx.tenant_id == "test-domain"
        assert ctx.connection_params["host"] == "localhost"
        assert ctx.connection_params["dbname"] == "scout"
        assert "search_path=dimagi" in ctx.connection_params["options"]

    async def test_raises_when_no_active_schema(self, tenant_membership):
        with pytest.raises(ValueError, match="No active schema"):
            await load_tenant_context("dimagi", "commcare")

    async def test_raises_when_no_managed_db_url(self, tenant_membership):
        await TenantSchema.objects.acreate(
            tenant=tenant_membership.tenant,
            schema_name="dimagi",
            state=SchemaState.ACTIVE,
        )

        with override_settings(MANAGED_DATABASE_URL=""):
            with pytest.raises(ValueError, match="MANAGED_DATABASE_URL"):
                await load_tenant_context("test-domain", "commcare")

    async def test_provider_predicate_resolves_correct_tenant(self):
        """Two tenants sharing an external_id across providers must each resolve
        to their OWN schema — external_id alone is ambiguous (arch #235)."""
        connect = await Tenant.objects.acreate(
            provider="commcare_connect", external_id="123", canonical_name="Opp 123"
        )
        ocs = await Tenant.objects.acreate(
            provider="ocs", external_id="123", canonical_name="Bot 123"
        )
        await TenantSchema.objects.acreate(
            tenant=connect, schema_name="connect_123", state=SchemaState.ACTIVE
        )
        await TenantSchema.objects.acreate(
            tenant=ocs, schema_name="ocs_123", state=SchemaState.ACTIVE
        )

        with override_settings(MANAGED_DATABASE_URL="postgresql://u:p@localhost:5432/scout"):
            connect_ctx = await load_tenant_context("123", "commcare_connect")
            ocs_ctx = await load_tenant_context("123", "ocs")

        assert connect_ctx.schema_name == "connect_123"
        assert ocs_ctx.schema_name == "ocs_123"


# ---------------------------------------------------------------------------
# _parse_db_url
# ---------------------------------------------------------------------------


class TestParseDbUrl:
    """Test the URL parser that builds connection params."""

    def test_full_url(self):

        params = _parse_db_url("postgresql://myuser:mypass@dbhost:5433/mydb", "tenant_schema")

        assert params["host"] == "dbhost"
        assert params["port"] == 5433
        assert params["dbname"] == "mydb"
        assert params["user"] == "myuser"
        assert params["password"] == "mypass"
        assert "search_path=tenant_schema,public" in params["options"]

    def test_defaults_for_missing_fields(self):

        params = _parse_db_url("postgresql://localhost/scout", "my_schema")

        assert params["host"] == "localhost"
        assert params["port"] == 5432
        assert params["dbname"] == "scout"
        assert params["user"] == ""
        assert params["password"] == ""
        assert params["sslmode"] == "prefer"

    def test_preserves_explicit_sslmode(self):

        params = _parse_db_url("postgresql://localhost/scout?sslmode=require", "my_schema")

        assert params["sslmode"] == "require"

    def test_bare_dbname_fallback(self):
        """In dev, MANAGED_DATABASE_URL may be just a database name."""

        params = _parse_db_url("scout", "my_schema")

        # urlparse("scout") gives path="scout", no host/port
        assert params["host"] == "localhost"
        assert params["port"] == 5432
        assert params["dbname"] == "scout"


# ---------------------------------------------------------------------------
# get_schema_status tool
# ---------------------------------------------------------------------------

PATCH_TENANT_SCHEMA = "apps.workspaces.models.TenantSchema"
PATCH_MATERIALIZATION_RUN = "apps.workspaces.models.MaterializationRun"


@pytest.mark.django_db(transaction=True)
class TestGetSchemaStatusTool:
    """Test the get_schema_status MCP tool."""

    async def test_requires_workspace_id(self):

        result = await get_schema_status()

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.django_db
    async def test_returns_not_found_when_workspace_missing(self):
        # 07#7: a non-existent workspace is NOT the same as an existing-but-
        # unprovisioned one. Returning the empty 'not_provisioned' success
        # envelope for a phantom workspace invited the agent to materialize
        # against a workspace that doesn't exist. Surface a NOT_FOUND error so
        # the agent stops instead of looping on a non-existent target.

        result = await get_schema_status(workspace_id="00000000-0000-0000-0000-000000000000")

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND

    @pytest.mark.django_db(transaction=True)
    async def test_returns_not_provisioned_when_workspace_exists_without_schema(self):
        # An existing workspace with no tenants/schema is genuinely
        # unprovisioned — that path must still report not_provisioned so the
        # agent can offer to materialize.

        User = get_user_model()
        user = await User.objects.acreate_user(email="noschema@b.c", password="x")
        ws = await Workspace.objects.acreate(name="empty-ws", created_by=user)

        result = await get_schema_status(workspace_id=str(ws.id))

        assert result["success"] is True
        assert result["data"]["exists"] is False
        assert result["data"]["state"] == "not_provisioned"

    @pytest.mark.django_db(transaction=True)
    async def test_returns_failed_with_error_for_failed_view_schema(self):
        """Multi-tenant workspace whose WorkspaceViewSchema is FAILED must
        return a TOP-LEVEL error envelope (success=False), not a misleading
        success-on-failure (arch #246, 13#6). A success envelope carrying
        ``data.error`` is mis-classified as a success by both the agent (the
        "agent told completed" class) and the rich cards."""
        User = get_user_model()
        user = await User.objects.acreate_user(email="vsfail@b.c", password="x")
        ws = await Workspace.objects.acreate(name="W-vsfail", created_by=user)
        for i in (1, 2):
            tenant = await Tenant.objects.acreate(
                external_id=f"vsfail-t{i}",
                provider="commcare",
                canonical_name=f"VSFail {i}",
            )
            await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
        await WorkspaceViewSchema.objects.acreate(
            workspace=ws,
            schema_name="ws_vsfailaaaaaaaa",
            state=SchemaState.FAILED,
            last_error="View name collision: 'a__t' produced by both tenants",
        )

        result = await get_schema_status(workspace_id=str(ws.id))

        assert result["success"] is False
        error = result["error"]
        # The collision text must survive so the agent and the card can show it.
        assert "View name collision" in (error["message"] + str(error.get("detail", "")))


# ---------------------------------------------------------------------------
# teardown_schema tool
# ---------------------------------------------------------------------------

PATCH_SCHEMA_MANAGER = "apps.workspaces.services.schema_manager.SchemaManager"


@pytest.mark.django_db(transaction=True)
class TestTeardownSchemaTool:
    """Test the teardown_schema MCP tool."""

    async def test_requires_confirm_true(self):

        result = await teardown_schema(confirm=False, workspace_id="ws-123")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR
        assert "confirm=True" in result["error"]["message"]

    async def test_default_confirm_is_false(self):

        result = await teardown_schema(workspace_id="ws-123")

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    async def test_requires_workspace_id(self):

        result = await teardown_schema(confirm=True)

        assert result["success"] is False
        assert result["error"]["code"] == VALIDATION_ERROR

    @pytest.mark.django_db
    async def test_not_found_when_no_workspace(self):

        result = await teardown_schema(
            confirm=True, workspace_id="00000000-0000-0000-0000-000000000000"
        )

        assert result["success"] is False
        assert result["error"]["code"] == NOT_FOUND


@pytest.mark.django_db(transaction=True)
class TestListPipelines:
    def test_returns_available_pipelines(self):

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

            result = asyncio.run(list_pipelines())

        assert result["success"] is True
        assert len(result["data"]["pipelines"]) == 1
        assert result["data"]["pipelines"][0]["name"] == "commcare_sync"
        assert result["data"]["pipelines"][0]["provider"] == "commcare"


@pytest.mark.django_db(transaction=True)
class TestGetMaterializationStatus:
    def test_returns_run_status(self):

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = uuid.UUID(run_id)
        mock_run.pipeline = "commcare_sync"
        mock_run.state = "completed"
        mock_run.started_at.isoformat.return_value = "2026-02-24T10:00:00+00:00"
        mock_run.completed_at.isoformat.return_value = "2026-02-24T10:05:00+00:00"
        mock_run.result = {"sources": {"cases": {"rows": 100}}}
        # Real model path: TenantSchema.tenant -> Tenant.external_id (12#0 item 3).
        # Prod reads run.tenant_schema.tenant.external_id; there is NO
        # tenant_membership on TenantSchema. Delete the fabricated wrong path so
        # a regression reading it raises AttributeError instead of silently
        # returning a MagicMock.
        mock_run.tenant_schema.tenant.external_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"
        del mock_run.tenant_schema.tenant_membership

        with (
            patch("mcp_server.server.MaterializationRun") as mock_cls,
            # The run-belongs-to-workspace scoping (arch #253, 01#6) is exercised
            # by tests/test_mcp_entitlement.py; here we isolate the status logic.
            patch(
                "mcp_server.server._run_belongs_to_workspace",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)

            result = asyncio.run(get_materialization_status(run_id=run_id, workspace_id="ws-1"))

        assert result["success"] is True
        assert result["data"]["run_id"] == run_id
        assert result["data"]["state"] == "completed"
        # Pin that tenant_id was read from the real attribute path.
        assert result["data"]["tenant_id"] == "dimagi"

    def test_unknown_run_returns_not_found(self):

        with patch("mcp_server.server.MaterializationRun") as mock_cls:
            mock_cls.DoesNotExist = ObjectDoesNotExist
            mock_cls.objects.select_related.return_value.aget = AsyncMock(
                side_effect=ObjectDoesNotExist
            )

            result = asyncio.run(get_materialization_status(run_id=str(uuid.uuid4())))

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"


@pytest.mark.django_db(transaction=True)
class TestCancelMaterialization:
    def test_cancel_in_progress_run(self):

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.id = uuid.UUID(run_id)
        mock_run.state = "loading"
        mock_run.result = {}
        # No procrastinate job here — the abort / ThreadJob flip is exercised in a
        # dedicated real-models test below (arch #255, 01#1).
        mock_run.procrastinate_job_id = None
        # Real model path: TenantSchema.tenant -> Tenant.external_id (12#0 item 3).
        mock_run.tenant_schema.tenant.external_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"
        del mock_run.tenant_schema.tenant_membership
        mock_run.asave = AsyncMock()

        with (
            patch("mcp_server.server.MaterializationRun") as mock_cls,
            patch(
                "mcp_server.server._authorize_materialization_write",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "mcp_server.server._materialization_write_access",
                new=AsyncMock(return_value=WorkspaceAccess(workspace=object())),
            ),
            patch(
                "mcp_server.server._run_belongs_to_workspace",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            mock_cls.ACTIVE_STATES = MaterializationRun.ACTIVE_STATES
            mock_cls.RunState.STARTED = "started"
            mock_cls.RunState.DISCOVERING = "discovering"
            mock_cls.RunState.LOADING = "loading"
            mock_cls.RunState.TRANSFORMING = "transforming"
            mock_cls.RunState.FAILED = "failed"
            mock_cls.RunState.CANCELLED = "cancelled"

            result = asyncio.run(
                cancel_materialization(
                    run_id=run_id, workspace_id="ws-1", user_id="authorized-user"
                )
            )

        assert result["success"] is True
        assert result["data"]["cancelled"] is True
        assert result["data"]["run_id"] == run_id
        assert result["data"]["previous_state"] == "loading"
        # #290: cancel_materialization now writes the dedicated RunState.CANCELLED
        # (matching the user-facing cancel_thread_job endpoint) instead of FAILED,
        # so a deliberately-cancelled run no longer masquerades as a failure in
        # state-based reporting. The response still carries result.cancelled=True
        # for back-compat.
        assert mock_run.state == "cancelled"
        assert mock_run.result == {"cancelled": True}
        mock_run.asave.assert_awaited_once_with(update_fields=["state", "completed_at", "result"])

    def test_cancel_completed_run_returns_error(self):

        run_id = str(uuid.uuid4())
        mock_run = MagicMock()
        mock_run.state = "completed"
        # Real model path: TenantSchema.tenant -> Tenant.external_id (12#0 item 3).
        mock_run.tenant_schema.tenant.external_id = "dimagi"
        mock_run.tenant_schema.schema_name = "dimagi"
        del mock_run.tenant_schema.tenant_membership

        with (
            patch("mcp_server.server.MaterializationRun") as mock_cls,
            patch(
                "mcp_server.server._authorize_materialization_write",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "mcp_server.server._materialization_write_access",
                new=AsyncMock(return_value=WorkspaceAccess(workspace=object())),
            ),
            patch(
                "mcp_server.server._run_belongs_to_workspace",
                new=AsyncMock(return_value=True),
            ),
        ):
            mock_cls.objects.select_related.return_value.aget = AsyncMock(return_value=mock_run)
            mock_cls.ACTIVE_STATES = MaterializationRun.ACTIVE_STATES
            mock_cls.RunState.STARTED = "started"
            mock_cls.RunState.DISCOVERING = "discovering"
            mock_cls.RunState.LOADING = "loading"
            mock_cls.RunState.TRANSFORMING = "transforming"
            mock_cls.RunState.FAILED = "failed"

            result = asyncio.run(
                cancel_materialization(
                    run_id=run_id, workspace_id="ws-1", user_id="authorized-user"
                )
            )

        assert result["success"] is False
        assert "not in progress" in result["error"]["message"].lower()


@pytest.mark.django_db(transaction=True)
async def test_cancel_materialization_aborts_job_and_flips_threadjob():
    """arch #255, 01#1: a mid-load MCP cancel must not only write CANCELLED but
    also abort the procrastinate job and flip the owning chat ThreadJob, so the
    load actually unwinds and the chat spinner clears (matching the HTTP cancel
    path). Before the fix it left the job running and the ThreadJob active."""

    User = get_user_model()
    user = await User.objects.acreate_user(email="mcpcancel@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-mcpcancel", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="mcpc", provider="commcare", canonical_name="MCPC"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="s_mcpc", state=SchemaState.ACTIVE
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=990011,
        tool_call_id="tc-mcpc",
        state=ThreadJob.State.PENDING,
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=990011,
    )

    abort = AsyncMock(return_value=None)
    with patch("mcp_server.server.procrastinate_app") as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = abort
        result = await cancel_materialization(
            run_id=str(run.id), workspace_id=str(ws.id), user_id=str(user.id)
        )

    assert result["success"] is True
    abort.assert_awaited_once_with(990011, abort=True)
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.CANCELLED
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.CANCELLED
    assert tj.completed_at is not None


# ---------------------------------------------------------------------------
# Integration tests — require a real PostgreSQL connection
# ---------------------------------------------------------------------------


class TestExecuteAsyncIntegration:
    """
    End-to-end tests for _execute_async_parameterized against
    a real PostgreSQL server.  These catch driver-level regressions (e.g. SET
    statement_timeout failing with psycopg3's server-side parameters) that
    mock-based tests can't detect.

    Uses MANAGED_DATABASE_URL, which CI requires via test_ci_integrity.
    """

    @pytest.fixture(autouse=True)
    def real_db(self):
        with managed_query_context() as ctx:
            self.query_context = ctx
            yield

    def _ctx(self):
        return self.query_context

    @pytest.mark.asyncio
    async def test_returns_rows(self):

        result = await _execute_async_parameterized(
            self._ctx(), "SELECT name, value FROM items ORDER BY value", (), 30
        )

        assert result["columns"] == ["name", "value"]
        assert result["rows"] == [["alpha", 1], ["beta", 2], ["gamma", 3]]
        assert result["row_count"] == 3

    @pytest.mark.asyncio
    async def test_statement_timeout_does_not_use_server_side_param(self):
        """Regression: SET statement_timeout TO $1 raises SyntaxError in psycopg3."""

        # Would raise psycopg.errors.SyntaxError before the fix
        result = await _execute_async_parameterized(self._ctx(), "SELECT 1 AS n", (), 30)
        assert result["row_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_result(self):

        result = await _execute_async_parameterized(
            self._ctx(), "SELECT name FROM items WHERE value > 9999", (), 30
        )

        assert result["columns"] == ["name"]
        assert result["rows"] == []
        assert result["row_count"] == 0

    @pytest.mark.asyncio
    async def test_parameterized_filters_rows(self):

        result = await _execute_async_parameterized(
            self._ctx(),
            "SELECT name, value FROM items WHERE value > %s ORDER BY value",
            (1,),
            30,
        )

        assert result["columns"] == ["name", "value"]
        assert result["rows"] == [["beta", 2], ["gamma", 3]]
        assert result["row_count"] == 2

    @pytest.mark.asyncio
    async def test_agent_sql_can_use_like_patterns(self):
        """Free-text search is the whole point of re-enabling raw SQL (issue #406).
        A literal '%' must survive to the server: psycopg only skips placeholder
        parsing when params is None, and an empty tuple is not None."""
        result = await execute_query(
            self._ctx(), "SELECT name FROM items WHERE name LIKE '%a%' ORDER BY name"
        )

        assert result["columns"] == ["name"]
        assert result["rows"] == [["alpha"], ["beta"], ["gamma"]]
        assert "items" in result["tables_accessed"]

    @pytest.mark.asyncio
    async def test_search_path_is_applied(self):
        """Unqualified table name resolves because search_path is set to the schema."""

        # No schema qualifier — relies on SET search_path TO working correctly
        result = await _execute_async_parameterized(
            self._ctx(), "SELECT count(*) AS n FROM items", (), 30
        )

        assert result["row_count"] == 1
        assert result["rows"][0][0] == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("view_state", [SchemaState.ACTIVE])
@pytest.mark.parametrize("malformed", [False, True])
async def test_schema_status_discloses_missing_sources(user, view_state, malformed):
    workspace = await Workspace.objects.acreate(name="Degraded workspace", created_by=user)
    tenants = []
    for suffix in ("ready", "missing"):
        tenant = await Tenant.objects.acreate(provider="commcare", external_id=suffix)
        tenants.append(tenant)
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    coverage = {
        "included_tenants": [{"tenant_id": str(tenants[0].id), "external_id": "ready"}],
        "excluded_tenants": [{"tenant_id": str(tenants[1].id), "external_id": "missing"}],
    }
    if malformed:
        coverage = {"excluded_tenants": [None]}
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="ws_degraded_status",
        state=view_state,
        tenant_coverage=coverage,
    )
    with (
        patch("mcp_server.server._resolve_mcp_context", AsyncMock()),
        patch("mcp_server.server.workspace_list_tables", AsyncMock(return_value=[])),
    ):
        result = await get_schema_status(workspace_id=str(workspace.id))
    assert result["success"] is True
    assert result["data"]["tenant_coverage"] == (
        coverage if view_state == SchemaState.ACTIVE else None
    )
    assert result["data"]["data_complete"] is (
        False if view_state == SchemaState.ACTIVE and not malformed else None
    )


async def _tenant_in(workspace, name):
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id=name, canonical_name=name
    )
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    return tenant


async def _run(
    tenant, state, job_id, *, schema_state=SchemaState.ACTIVE, progress=None, loaded_by=None
):
    """A run on the tenant's schema; ``loaded_by`` makes it that workspace's load candidate."""
    if loaded_by is not None:
        assert schema_state == SchemaState.ACTIVE, "a load candidate is always PROVISIONING"
        schema = await TenantSchema.objects.acreate(
            tenant=tenant,
            schema_name=f"c_{tenant.external_id}_{job_id}",
            state=SchemaState.PROVISIONING,
            load_workspace_id=loaded_by.id,
        )
    else:
        schema = await TenantSchema.objects.filter(tenant=tenant).afirst()
    if schema is None:
        schema = await TenantSchema.objects.acreate(
            tenant=tenant, schema_name=f"s_{tenant.external_id}", state=schema_state
        )
    return await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=state,
        procrastinate_job_id=job_id,
        progress=progress,
    )


@pytest.mark.django_db(transaction=True)
async def test_schema_status_reports_which_source_an_in_flight_load_is_on(user):
    """#411: while a multi-source load runs, report "source 2 of 3", not only stale state."""
    workspace = await Workspace.objects.acreate(name="Loading workspace", created_by=user)
    done, loading, waiting = [
        await _tenant_in(workspace, name) for name in ("alpha", "bravo", "charlie")
    ]
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name="ws_loading_status", state=SchemaState.ACTIVE
    )
    # An earlier job's run must not count as progress through the current one.
    await _run(waiting, MaterializationRun.RunState.COMPLETED, 41)
    await _run(done, MaterializationRun.RunState.COMPLETED, 42)
    await _run(
        loading,
        MaterializationRun.RunState.LOADING,
        42,
        loaded_by=workspace,
        progress={
            "step": 3,
            "total_steps": 5,
            "source": "visits",
            "message": "Loading visits from commcare API...",
            "rows_loaded": 100,
            "rows_total": 400,
            "unit": "rows",
        },
    )

    with (
        patch("mcp_server.server._resolve_mcp_context", AsyncMock()),
        patch("mcp_server.server.workspace_list_tables", AsyncMock(return_value=[])),
    ):
        result = await get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    in_flight = result["data"]["load_in_progress"]
    assert in_flight["sources_total"] == 3
    assert in_flight["sources_finished"] == 1
    assert "source 2 of 3" in in_flight["message"]
    assert "bravo" in in_flight["message"]
    assert [(s["name"], s["state"]) for s in in_flight["sources"]] == [
        ("alpha", "completed"),
        ("bravo", "loading"),
        ("charlie", "waiting"),
    ]
    current = in_flight["sources"][1]
    assert current["step"] == 3
    assert current["total_steps"] == 5
    assert current["table"] == "visits"
    assert current["rows_loaded"] == 100
    assert current["rows_total"] == 400


@pytest.mark.django_db(transaction=True)
async def test_schema_status_reports_a_first_load_before_any_schema_is_active(user):
    """#411: a first load used to read as plain "not_provisioned" while it ran."""
    workspace = await Workspace.objects.acreate(name="First load", created_by=user)
    tenant = await _tenant_in(workspace, "solo")
    await _run(
        tenant,
        MaterializationRun.RunState.DISCOVERING,
        7,
        loaded_by=workspace,
    )

    result = await get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    assert result["data"]["state"] == "not_provisioned"
    in_flight = result["data"]["load_in_progress"]
    assert in_flight["sources_total"] == 1
    assert "source 1 of 1" in in_flight["message"]
    assert in_flight["sources"][0]["state"] == "discovering"


@pytest.mark.django_db(transaction=True)
async def test_schema_status_reports_no_load_when_every_run_has_finished(user):
    workspace = await Workspace.objects.acreate(name="Idle", created_by=user)
    tenant = await _tenant_in(workspace, "idle")
    await _run(tenant, MaterializationRun.RunState.COMPLETED, 9)

    with patch("mcp_server.server.pipeline_list_tables", AsyncMock(return_value=[])):
        result = await get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    assert result["data"]["load_in_progress"] is None


async def _schema_status(workspace):
    with (
        patch("mcp_server.server._resolve_mcp_context", AsyncMock()),
        patch("mcp_server.server.workspace_list_tables", AsyncMock(return_value=[])),
        patch("mcp_server.server.pipeline_list_tables", AsyncMock(return_value=[])),
    ):
        result = await get_schema_status(workspace_id=str(workspace.id))
    assert result["success"] is True
    return result["data"]["load_in_progress"]


@pytest.mark.django_db(transaction=True)
async def test_schema_status_ignores_another_workspaces_load_of_a_shared_source(user):
    """G5: a sibling loading a shared tenant is not this workspace's load."""
    workspace = await Workspace.objects.acreate(name="Idle sharer", created_by=user)
    sibling = await Workspace.objects.acreate(name="Loading sharer", created_by=user)
    shared = await _tenant_in(workspace, "shared")
    await _tenant_in(workspace, "own")
    await WorkspaceTenant.objects.acreate(workspace=sibling, tenant=shared)
    await _run(shared, MaterializationRun.RunState.LOADING, 50, loaded_by=sibling)

    assert await _schema_status(workspace) is None


@pytest.mark.django_db(transaction=True)
async def test_schema_status_reports_waiting_on_another_workspaces_load_apart(user):
    """G5: when this workspace's load waits on a sibling's load, say so distinctly."""
    workspace = await Workspace.objects.acreate(name="Waiting sharer", created_by=user)
    sibling = await Workspace.objects.acreate(name="Loading sharer", created_by=user)
    loaded = await _tenant_in(workspace, "alpha")
    shared = await _tenant_in(workspace, "bravo")
    await WorkspaceTenant.objects.acreate(workspace=sibling, tenant=shared)
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=60,
        state=ThreadJob.State.PENDING,
    )
    await _run(loaded, MaterializationRun.RunState.COMPLETED, 60)
    await _run(shared, MaterializationRun.RunState.LOADING, 61, loaded_by=sibling)

    in_flight = await _schema_status(workspace)

    assert in_flight["sources_finished"] == 1
    assert "Loading source" not in in_flight["message"]
    assert "waiting for another workspace" in in_flight["message"]
    assert "bravo" in in_flight["message"]
    assert [(s["name"], s["state"]) for s in in_flight["sources"]] == [
        ("alpha", "completed"),
        ("bravo", "loading_in_other_workspace"),
    ]


@pytest.mark.django_db(transaction=True)
async def test_schema_status_names_a_shared_source_its_own_load_has_yet_to_reach(user):
    """G5: our load still counts only its own sources, and names the one it will wait on."""
    workspace = await Workspace.objects.acreate(name="Loading sharer", created_by=user)
    sibling = await Workspace.objects.acreate(name="Other sharer", created_by=user)
    loading = await _tenant_in(workspace, "alpha")
    shared = await _tenant_in(workspace, "bravo")
    await WorkspaceTenant.objects.acreate(workspace=sibling, tenant=shared)
    await _run(loading, MaterializationRun.RunState.LOADING, 100, loaded_by=workspace)
    await _run(shared, MaterializationRun.RunState.LOADING, 101, loaded_by=sibling)

    in_flight = await _schema_status(workspace)

    assert in_flight["message"].startswith("Loading source 1 of 2 (alpha)")
    assert (
        "Also waiting on another workspace's load of shared source(s): bravo"
        in (in_flight["message"])
    )
    assert [(s["name"], s["state"]) for s in in_flight["sources"]] == [
        ("alpha", "loading"),
        ("bravo", "loading_in_other_workspace"),
    ]


@pytest.mark.django_db(transaction=True)
async def test_schema_status_does_not_sequence_a_single_source_refresh(user):
    """G5: a refresh of one source is not "source 1 of N" of a workspace load."""
    workspace = await Workspace.objects.acreate(name="Refreshing", created_by=user)
    refreshed = await _tenant_in(workspace, "alpha")
    await _tenant_in(workspace, "bravo")
    schema = await TenantSchema.objects.acreate(
        tenant=refreshed,
        schema_name="r_alpha",
        state=SchemaState.PROVISIONING,
        refresh_workspace_id=workspace.id,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=70,
    )

    in_flight = await _schema_status(workspace)

    assert "Loading source" not in in_flight["message"]
    assert in_flight["message"].startswith("1 of 2 sources loading")


@pytest.mark.django_db(transaction=True)
async def test_schema_status_counts_a_failed_source_as_failed_not_finished(user):
    """G6: a source whose run failed landed nothing; it is not "finished"."""
    workspace = await Workspace.objects.acreate(name="Partly failed", created_by=user)
    failed, loading, _waiting = [
        await _tenant_in(workspace, name) for name in ("alpha", "bravo", "charlie")
    ]
    await _run(failed, MaterializationRun.RunState.FAILED, 80)
    await _run(loading, MaterializationRun.RunState.LOADING, 80, loaded_by=workspace)

    in_flight = await _schema_status(workspace)

    assert in_flight["sources_finished"] == 0
    assert in_flight["sources_failed"] == 1
    assert "source 2 of 3" in in_flight["message"]
    assert "1 failed" in in_flight["message"]


@pytest.mark.django_db(transaction=True)
async def test_schema_status_shows_a_zero_row_counter(user):
    """G7: the "0 of 400 rows" a load reports at its start must not be hidden."""
    workspace = await Workspace.objects.acreate(name="Starting", created_by=user)
    tenant = await _tenant_in(workspace, "solo")
    await _run(
        tenant,
        MaterializationRun.RunState.LOADING,
        90,
        loaded_by=workspace,
        progress={"rows_loaded": 0, "rows_total": 400, "unit": "rows"},
    )

    in_flight = await _schema_status(workspace)

    assert "(0 of 400 rows)" in in_flight["message"]


@pytest.mark.django_db(transaction=True)
async def test_schema_status_ignores_a_sibling_load_once_our_load_is_resuming(user):
    """G5: a RUNNING ThreadJob is the chat resume, after our materialization ended."""
    workspace = await Workspace.objects.acreate(name="Resuming sharer", created_by=user)
    sibling = await Workspace.objects.acreate(name="Loading sharer", created_by=user)
    await _tenant_in(workspace, "alpha")
    shared = await _tenant_in(workspace, "bravo")
    await WorkspaceTenant.objects.acreate(workspace=sibling, tenant=shared)
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=110,
        state=ThreadJob.State.RUNNING,
    )
    await _run(shared, MaterializationRun.RunState.LOADING, 111, loaded_by=sibling)

    assert await _schema_status(workspace) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", sorted(MaterializationRun.RunState.values))
async def test_cancel_accepts_exactly_the_active_run_states(state):
    """Cancellable means MaterializationRun.ACTIVE_STATES, not a local copy of it (#251)."""
    run = MagicMock(state=state, result={}, procrastinate_job_id=None)
    run.asave = AsyncMock()
    with (
        patch("mcp_server.server.MaterializationRun.objects") as objects,
        patch(
            "mcp_server.server._authorize_materialization_write",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "mcp_server.server._materialization_write_access",
            new=AsyncMock(return_value=WorkspaceAccess(workspace=object())),
        ),
        patch("mcp_server.server._run_belongs_to_workspace", new=AsyncMock(return_value=True)),
        patch("mcp_server.server._run_started_by", new=AsyncMock(return_value=True)),
    ):
        objects.select_related.return_value.aget = AsyncMock(return_value=run)
        result = await cancel_materialization(
            run_id=str(uuid.uuid4()), workspace_id="ws-1", user_id="authorized-user"
        )

    assert result["success"] is (state in MaterializationRun.ACTIVE_STATES)
