from unittest.mock import AsyncMock, MagicMock, patch, sentinel

import pytest


@pytest.mark.asyncio
async def test_query_tool_uses_workspace_context_when_workspace_id_provided():
    """When workspace_id is provided, query should call load_workspace_context."""
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
                sql="SELECT 1",
                workspace_id="some-workspace-uuid",
            )

    mock_lwc.assert_called_once_with("some-workspace-uuid")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_query_tool_requires_workspace_id():
    """When workspace_id is empty, query should return a validation error."""
    from mcp_server.server import query

    result = await query(sql="SELECT 1", workspace_id="")
    assert result["success"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_resolve_context_routes_to_workspace():
    """_resolve_mcp_context routes to load_workspace_context."""
    with patch("mcp_server.server.load_workspace_context", new_callable=AsyncMock) as mock_wctx:
        mock_wctx.return_value = sentinel
        from mcp_server.server import _resolve_mcp_context

        result = await _resolve_mcp_context("wid-123")
    mock_wctx.assert_called_once_with("wid-123")
    assert result is sentinel


@pytest.mark.asyncio
async def test_resolve_context_raises_on_empty_workspace_id():
    """_resolve_mcp_context raises ValueError when workspace_id is empty."""
    from mcp_server.server import _resolve_mcp_context

    with pytest.raises(ValueError, match="workspace_id is required"):
        await _resolve_mcp_context("")
