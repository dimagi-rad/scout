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
        mock_tool.name = "query"
        mock_client.get_tools.return_value = [mock_tool]

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client):
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                tools = await mod.get_mcp_tools()

        assert len(tools) == 1
        assert tools[0].name == "query"
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
    async def test_get_mcp_tools_passes_callback_to_client(self):
        """on_progress callback is forwarded to Callbacks(on_progress=...)."""
        from langchain_mcp_adapters.callbacks import Callbacks

        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        async def my_callback(progress, total, message, context):
            pass

        with (
            patch(
                "apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client
            ) as MockCls,
            patch.object(mod, "settings") as mock_settings,
        ):
            mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
            await mod.get_mcp_tools(on_progress=my_callback)

        _, kwargs = MockCls.call_args
        assert isinstance(kwargs.get("callbacks"), Callbacks)
        assert kwargs["callbacks"].on_progress is my_callback
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_no_callback_passes_none(self):
        """Without on_progress, callbacks kwarg is None."""
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

        _, kwargs = MockCls.call_args
        assert kwargs.get("callbacks") is None
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
