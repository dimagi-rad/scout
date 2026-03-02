"""Tests for Connect pipeline integration with MCP server credential resolution."""

from mcp_server.pipeline_registry import PipelineRegistry, get_registry


class TestConnectPipelineIntegration:
    def test_connect_sync_resolves_with_correct_provider(self):
        """The connect_sync pipeline should resolve with provider='commcare_connect'."""
        registry = PipelineRegistry()
        config = registry.get("connect_sync")
        assert config is not None
        assert config.provider == "commcare_connect"

    def test_commcare_sync_resolves_with_correct_provider(self):
        """The commcare_sync pipeline should resolve with provider='commcare'."""
        registry = PipelineRegistry()
        config = registry.get("commcare_sync")
        assert config is not None
        assert config.provider == "commcare"

    def test_list_pipelines_includes_both(self):
        """list() should include both commcare_sync and connect_sync."""
        registry = PipelineRegistry()
        names = [p.name for p in registry.list()]
        assert "commcare_sync" in names
        assert "connect_sync" in names

    def test_get_registry_singleton_contains_connect(self):
        """The global registry singleton should contain the connect_sync pipeline."""
        registry = get_registry()
        config = registry.get("connect_sync")
        assert config is not None
        assert config.provider == "commcare_connect"
        assert len(config.sources) == 7
