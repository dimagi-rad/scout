from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_query_tool_uses_workspace_context_when_workspace_id_provided():
    """When workspace_id is non-empty, query should call load_workspace_context."""
    mock_ctx = MagicMock()
    mock_ctx.schema_name = "ws_abc123"
    mock_ctx.max_rows_per_query = 500
    mock_ctx.max_query_timeout_seconds = 30

    with patch("mcp_server.server.load_workspace_context", new_callable=AsyncMock) as mock_lwc:
        mock_lwc.return_value = mock_ctx
        with patch("mcp_server.server.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "success": True,
                "columns": ["id"],
                "rows": [["1"]],
                "row_count": 1,
                "truncated": False,
                "sql_executed": "SELECT 1",
                "tables_accessed": [],
            }
            from mcp_server.server import query

            result = await query(
                tenant_id="old-tenant",
                sql="SELECT 1",
                workspace_id="some-workspace-uuid",
            )

    mock_lwc.assert_called_once_with("some-workspace-uuid")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_query_tool_falls_back_to_tenant_context_when_no_workspace_id():
    """When workspace_id is empty, query should call load_tenant_context."""
    mock_ctx = MagicMock()
    mock_ctx.schema_name = "tenant_schema"
    mock_ctx.max_rows_per_query = 500
    mock_ctx.max_query_timeout_seconds = 30

    with patch("mcp_server.server.load_tenant_context", new_callable=AsyncMock) as mock_ltc:
        mock_ltc.return_value = mock_ctx
        with patch("mcp_server.server.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {
                "success": True,
                "columns": ["id"],
                "rows": [["1"]],
                "row_count": 1,
                "truncated": False,
                "sql_executed": "SELECT 1",
                "tables_accessed": [],
            }
            from mcp_server.server import query

            result = await query(tenant_id="my-tenant", sql="SELECT 1", workspace_id="")

    mock_ltc.assert_called_once_with("my-tenant")
    assert result["success"] is True
