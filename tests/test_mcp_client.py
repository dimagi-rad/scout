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
        mod.reset_tools_cache()

        mock_client = AsyncMock()
        mock_tool = AsyncMock()
        mock_tool.name = "query"
        mock_client.get_tools.return_value = [mock_tool]

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client):
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                mock_settings.MCP_SHARED_SECRET = ""
                tools = await mod.get_mcp_tools()

        assert len(tools) == 1
        assert tools[0].name == "query"
        mock_client.get_tools.assert_awaited_once()
        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_caches_static_tool_schemas(self):
        """get_mcp_tools caches the tool list (schemas are static) so a second
        call does NOT do another tools/list HTTP round trip (arch #253, 10#1)."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

        mock_client = AsyncMock()
        mock_tool = AsyncMock()
        mock_tool.name = "query"
        mock_client.get_tools.return_value = [mock_tool]

        with (
            patch(
                "apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client
            ) as MockCls,
            patch.object(mod, "settings") as mock_settings,
        ):
            mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
            mock_settings.MCP_SHARED_SECRET = ""
            first = await mod.get_mcp_tools()
            second = await mod.get_mcp_tools()

        assert first == second
        # Second call reuses the cache: no second client construction / tools/list.
        assert MockCls.call_count == 1
        mock_client.get_tools.assert_awaited_once()
        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_sends_shared_secret_header(self):
        """The MCP client sends the shared secret in the connection headers so the
        server's SharedSecretMiddleware accepts the call (arch #253, 01#6)."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        with (
            patch(
                "apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client
            ) as MockCls,
            patch.object(mod, "settings") as mock_settings,
        ):
            mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
            mock_settings.MCP_SHARED_SECRET = "s3cr3t"
            await mod.get_mcp_tools()

        conn = MockCls.call_args.args[0]["scout-data"]
        assert conn["headers"]["X-Scout-MCP-Secret"] == "s3cr3t"
        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_omits_header_when_secret_unset(self):
        """No header is sent when the secret is unset (dev fail-open path)."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        with (
            patch(
                "apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client
            ) as MockCls,
            patch.object(mod, "settings") as mock_settings,
        ):
            mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
            mock_settings.MCP_SHARED_SECRET = ""
            await mod.get_mcp_tools()

        conn = MockCls.call_args.args[0]["scout-data"]
        assert "headers" not in conn or not conn["headers"]
        mod.reset_circuit_breaker()
        mod.reset_tools_cache()

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
