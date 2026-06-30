"""
Tests for MCP client integration.

Covers:
- MCP client creation per request
- Circuit breaker behavior
- Callback forwarding
"""

from unittest.mock import AsyncMock, patch

import pytest

# --- MCP client tests ---


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_get_mcp_tools_returns_tools(self):
        """get_mcp_tools creates a client and returns its tools."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_tool = AsyncMock()
        mock_tool.name = "semantic_query"
        mock_client.get_tools.return_value = [mock_tool]

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client):
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                tools = await mod.get_mcp_tools()

        assert len(tools) == 1
        assert tools[0].name == "semantic_query"
        mock_client.get_tools.assert_awaited_once()
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_creates_new_client_each_call(self):
        """get_mcp_tools creates a fresh client on each call (no singleton)."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        with (
            patch(
                "apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client
            ) as MockCls,
            patch.object(mod, "settings") as mock_settings,
        ):
            mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
            await mod.get_mcp_tools()
            await mod.get_mcp_tools()

        assert MockCls.call_count == 2
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self):
        """Circuit breaker raises MCPServerUnavailable after threshold failures."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        with patch("apps.agents.mcp_client.MultiServerMCPClient", side_effect=Exception("down")):
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                for _ in range(mod._CIRCUIT_BREAKER_THRESHOLD):
                    with pytest.raises(Exception, match="down"):
                        await mod.get_mcp_tools()

                with pytest.raises(mod.MCPServerUnavailable):
                    await mod.get_mcp_tools()

        mod.reset_circuit_breaker()
