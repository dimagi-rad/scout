import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg.errors
import pytest

from apps.common.capacity import BUSY_MESSAGE, CapacityResource
from mcp_server.context import QueryContext
from mcp_server.services.pool import ManagedPoolExhausted
from mcp_server.services.query import (
    _classify_error,
    _execute_async_parameterized,
    execute_query,
)


class TestQueryContextReadonlyRole:
    def test_readonly_role_derived_from_schema_name(self):
        ctx = QueryContext(
            tenant_id="test-domain",
            schema_name="test_domain",
            connection_params={"host": "localhost"},
        )
        assert ctx.readonly_role == "test_domain_ro"

    def test_readonly_role_view_schema(self):
        ctx = QueryContext(
            tenant_id="workspace-123",
            schema_name="ws_abc1234def56789",
            connection_params={"host": "localhost"},
        )
        assert ctx.readonly_role == "ws_abc1234def56789_ro"


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
    """Mock AsyncConnectionPool whose .connection() yields ``mock_conn`` — queries
    acquire from the shared managed-DB pool now (arch #253, 10#1)."""
    pool = MagicMock()
    pool.connection.return_value = mock_conn
    return AsyncMock(return_value=pool)


class TestSetRoleIsolation:
    def _make_ctx(self, schema_name="test_domain"):
        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            connection_params={"host": "localhost"},
        )

    @pytest.mark.asyncio
    async def test_execute_async_parameterized_sets_and_resets_role(self):
        mock_cursor = AsyncMock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("val1",)]

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "mcp_server.services.query.get_pool",
            new=_make_pool_for_conn(mock_conn),
        ):
            ctx = self._make_ctx()
            await _execute_async_parameterized(ctx, "SELECT 1", (), 30)

        execute_calls = mock_cursor.execute.call_args_list
        # First call should be SET ROLE
        first_call_str = str(execute_calls[0])
        assert "SET ROLE" in first_call_str
        assert "test_domain_ro" in first_call_str
        # Cleanup before the pooled connection returns: RESET ROLE then RESET ALL.
        call_strs = [str(c) for c in execute_calls]
        assert any("RESET ROLE" in c for c in call_strs)
        assert "RESET ALL" in call_strs[-1]
        assert any("default_transaction_read_only" in call for call in call_strs)
        readonly_index = next(
            i for i, call in enumerate(call_strs) if "default_transaction_read_only" in call
        )
        query_index = next(i for i, call in enumerate(call_strs) if "SELECT 1" in call)
        assert readonly_index < query_index
        assert "on" in call_strs[readonly_index]

    @pytest.mark.asyncio
    async def test_agent_sql_runs_pooled_under_the_readonly_role(self):
        """Agent-authored SQL must reach the DB the same way internal SQL does:
        from the shared pool, under the tenant's read-only role (issue #406)."""
        mock_cursor = AsyncMock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("val1",)]

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "mcp_server.services.query.get_pool",
            new=_make_pool_for_conn(mock_conn),
        ):
            result = await execute_query(self._make_ctx(), "SELECT col1 FROM messages")

        assert result["row_count"] == 1
        call_strs = [str(c) for c in mock_cursor.execute.call_args_list]
        assert "SET ROLE" in call_strs[0]
        assert "test_domain_ro" in call_strs[0]
        assert any("LIMIT" in c.upper() for c in call_strs)
        assert any("RESET ROLE" in c for c in call_strs)
        assert "RESET ALL" in call_strs[-1]

    @pytest.mark.asyncio
    async def test_reset_role_on_query_error(self):
        mock_cursor = AsyncMock()
        mock_cursor.execute.side_effect = [
            None,  # SET ROLE succeeds
            None,  # SET search_path succeeds
            None,  # SET default_transaction_read_only succeeds
            None,  # SET statement_timeout succeeds
            Exception("query failed"),  # actual query fails
            None,  # RESET ROLE succeeds
            None,  # RESET ALL succeeds
        ]

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "mcp_server.services.query.get_pool",
            new=_make_pool_for_conn(mock_conn),
        ):
            ctx = self._make_ctx()
            with contextlib.suppress(Exception):
                await _execute_async_parameterized(ctx, "SELECT bad", (), 30)

        # RESET ROLE must still have run after the query error (before RESET ALL).
        call_strs = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any("RESET ROLE" in c for c in call_strs)


class TestRoleErrorClassification:
    def test_invalid_role_classified_as_connection_error(self):
        exc = psycopg.errors.InsufficientPrivilege("role 'test_domain_ro' does not exist")
        code, message = _classify_error(exc)
        assert code == "CONNECTION_ERROR"
        assert "administrator" in message.lower()


class TestCapacityClassification:
    @staticmethod
    async def _run_failing_query(exc):
        ctx = QueryContext(
            tenant_id="t",
            schema_name="t",
            connection_params={"host": "localhost"},
        )
        with (
            patch("mcp_server.services.query._execute_async_parameterized", side_effect=exc),
            patch("mcp_server.services.query.report_capacity_exhausted") as report,
        ):
            result = await execute_query(ctx, "SELECT 1")
        return result, report

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            psycopg.errors.TooManyConnections("sorry, too many clients already"),
            psycopg.OperationalError("FATAL: remaining connection slots are reserved"),
            ManagedPoolExhausted("couldn't get a connection after 30.00 sec"),
        ],
    )
    async def test_connection_limit_yields_busy_envelope_and_alert(self, exc):
        result, report = await self._run_failing_query(exc)

        assert result["success"] is False
        assert result["error"]["code"] == "CAPACITY_EXHAUSTED"
        assert result["error"]["message"] == BUSY_MESSAGE
        report.assert_called_once()
        assert report.call_args.args[0] == CapacityResource.DATABASE

    @pytest.mark.asyncio
    async def test_other_connection_errors_unchanged(self):
        result, report = await self._run_failing_query(
            psycopg.OperationalError("could not connect to server")
        )

        assert result["error"]["code"] == "CONNECTION_ERROR"
        assert "Could not connect" in result["error"]["message"]
        report.assert_not_called()
