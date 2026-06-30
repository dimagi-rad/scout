from unittest.mock import AsyncMock, patch, sentinel

import pytest


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
