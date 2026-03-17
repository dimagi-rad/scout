from unittest.mock import MagicMock, patch

from mcp_server.context import QueryContext
from mcp_server.services.query import _execute_sync


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


class TestSetRoleIsolation:
    def _make_ctx(self, schema_name="test_domain"):
        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            connection_params={"host": "localhost"},
        )

    @patch("mcp_server.services.query._get_connection")
    def test_execute_sync_sets_and_resets_role(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.description = [("col1",)]
        mock_cursor.fetchall.return_value = [("val1",)]
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        ctx = self._make_ctx()
        _execute_sync(ctx, "SELECT 1", 30)

        execute_calls = mock_cursor.execute.call_args_list
        # First call should be SET ROLE
        first_call_str = str(execute_calls[0])
        assert "SET ROLE" in first_call_str
        assert "test_domain_ro" in first_call_str
        # Last call before cursor.close should be RESET ROLE
        last_call_str = str(execute_calls[-1])
        assert "RESET ROLE" in last_call_str

    @patch("mcp_server.services.query._get_connection")
    def test_reset_role_on_query_error(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = [
            None,  # SET ROLE succeeds
            None,  # SET search_path succeeds
            None,  # SET statement_timeout succeeds
            Exception("query failed"),  # actual query fails
            None,  # RESET ROLE succeeds
        ]
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        ctx = self._make_ctx()
        try:
            _execute_sync(ctx, "SELECT bad", 30)
        except Exception:
            pass

        # RESET ROLE should still have been called
        last_call_str = str(mock_cursor.execute.call_args_list[-1])
        assert "RESET ROLE" in last_call_str
