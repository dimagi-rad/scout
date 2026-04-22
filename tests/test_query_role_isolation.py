import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg.errors
import pytest

from mcp_server.context import QueryContext
from mcp_server.services.query import _classify_error, _execute_async


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


class TestSetRoleIsolation:
    def _make_ctx(self, schema_name="test_domain"):
        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            connection_params={"host": "localhost"},
        )

    @pytest.mark.asyncio
    async def test_execute_async_sets_and_resets_role(self):
        mock_cursor = AsyncMock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("val1",)]

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "psycopg.AsyncConnection.connect",
            new=AsyncMock(return_value=mock_conn),
        ):
            ctx = self._make_ctx()
            await _execute_async(ctx, "SELECT 1", 30)

        execute_calls = mock_cursor.execute.call_args_list
        # First call should be SET ROLE
        first_call_str = str(execute_calls[0])
        assert "SET ROLE" in first_call_str
        assert "test_domain_ro" in first_call_str
        # Last call before cursor.close should be RESET ROLE
        last_call_str = str(execute_calls[-1])
        assert "RESET ROLE" in last_call_str

    @pytest.mark.asyncio
    async def test_reset_role_on_query_error(self):
        mock_cursor = AsyncMock()
        mock_cursor.execute.side_effect = [
            None,  # SET ROLE succeeds
            None,  # SET search_path succeeds
            None,  # SET statement_timeout succeeds
            Exception("query failed"),  # actual query fails
            None,  # RESET ROLE succeeds
        ]

        mock_conn = _make_async_conn(mock_cursor)

        with patch(
            "psycopg.AsyncConnection.connect",
            new=AsyncMock(return_value=mock_conn),
        ):
            ctx = self._make_ctx()
            with contextlib.suppress(Exception):
                await _execute_async(ctx, "SELECT bad", 30)

        # RESET ROLE should still have been called
        last_call_str = str(mock_cursor.execute.call_args_list[-1])
        assert "RESET ROLE" in last_call_str


class TestRoleErrorClassification:
    def test_invalid_role_classified_as_connection_error(self):
        exc = psycopg.errors.InsufficientPrivilege("role 'test_domain_ro' does not exist")
        code, message = _classify_error(exc)
        assert code == "CONNECTION_ERROR"
        assert "administrator" in message.lower()
