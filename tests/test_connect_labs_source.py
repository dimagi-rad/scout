"""Tests for the commcare_connect_labs synthetic data source.

Verifies that:
1. Connect loaders are instantiated with base_url=settings.CONNECT_LABS_API_URL
   when the provider is commcare_connect_labs (no live API required).
2. PipelineRegistry resolves a pipeline for provider commcare_connect_labs with
   the expected sources.

Live ingestion is gated on connect-labs#637 being deployed, a registered
synthetic opportunity, and CONNECT_LABS_API_URL + PAT configured.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_server.pipeline_registry import PipelineRegistry

# ── Pipeline registry tests ────────────────────────────────────────────────────


class TestConnectLabsPipelineRegistry:
    def test_registry_resolves_connect_labs_pipeline(self):
        """The real connect_labs_sync.yml loads and is discoverable by provider."""
        registry = PipelineRegistry()
        config = registry.get_by_provider("commcare_connect_labs")
        assert config is not None
        assert config.name == "connect_labs_sync"
        assert config.provider == "commcare_connect_labs"

    def test_connect_labs_pipeline_has_expected_sources(self):
        registry = PipelineRegistry()
        config = registry.get("connect_labs_sync")
        assert config is not None
        source_names = [s.name for s in config.sources]
        assert source_names == [
            "visits",
            "users",
            "completed_works",
            "payments",
            "invoices",
            "assessments",
            "completed_modules",
        ]

    def test_connect_labs_pipeline_has_metadata_discovery(self):
        registry = PipelineRegistry()
        config = registry.get("connect_labs_sync")
        assert config is not None
        assert config.has_metadata_discovery is True

    def test_connect_labs_pipeline_resumability_mirrors_connect_sync(self):
        """visits is resumable; all others are non-resumable (no per-row id)."""
        registry = PipelineRegistry()
        config = registry.get("connect_labs_sync")
        assert config is not None
        by_name = {s.name: s for s in config.sources}
        assert by_name["visits"].resumable is True
        for src_name in (
            "users",
            "completed_works",
            "payments",
            "invoices",
            "assessments",
            "completed_modules",
        ):
            assert by_name[src_name].resumable is False, (
                f"{src_name} must be non-resumable (no per-row id in export)"
            )

    def test_connect_labs_pipeline_has_relationships(self):
        registry = PipelineRegistry()
        config = registry.get("connect_labs_sync")
        assert config is not None
        assert len(config.relationships) == 5
        to_tables = {r.to_table for r in config.relationships}
        assert to_tables == {"raw_users"}


# ── Loader base_url threading tests ───────────────────────────────────────────


_TEST_LABS_URL = "https://connect-labs.example.com"

_CREDENTIAL = {"type": "api_key", "value": "test-pat-token"}


def _make_tenant_membership(opp_id="42"):
    tm = MagicMock()
    tm.tenant.external_id = opp_id
    return tm


class TestConnectLabsLoaderBaseUrl:
    """Assert that _load_connect_source passes base_url to each loader class."""

    def _run_load(self, source_name: str):
        """Patch the target loader class and return its call_args."""
        from mcp_server.services import materializer as mat

        tm = _make_tenant_membership()
        conn = MagicMock()
        conn.autocommit = False

        # Map source name → the attribute name on the materializer module
        loader_attr_map = {
            "visits": "ConnectVisitLoader",
            "users": "ConnectUserLoader",
            "completed_works": "ConnectCompletedWorkLoader",
            "payments": "ConnectPaymentLoader",
            "invoices": "ConnectInvoiceLoader",
            "assessments": "ConnectAssessmentLoader",
            "completed_modules": "ConnectCompletedModuleLoader",
        }
        loader_attr = loader_attr_map[source_name]

        with patch.object(mat, loader_attr) as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.load_pages.return_value = iter([])
            mock_loader_cls.return_value = mock_loader

            # Patch the corresponding writer to a no-op
            writer_attr = {
                "visits": "_write_connect_visits",
                "users": "_write_connect_users",
                "completed_works": "_write_connect_completed_works",
                "payments": "_write_connect_payments",
                "invoices": "_write_connect_invoices",
                "assessments": "_write_connect_assessments",
                "completed_modules": "_write_connect_completed_modules",
            }[source_name]
            with patch.object(mat, writer_attr, return_value=0):
                mat._load_connect_source(
                    source_name,
                    tm,
                    _CREDENTIAL,
                    "test_schema",
                    conn,
                    base_url=_TEST_LABS_URL,
                )
            return mock_loader_cls.call_args

    def test_visits_loader_receives_base_url(self):
        args = self._run_load("visits")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL or (
            len(args.args) >= 3 and args.args[2] == _TEST_LABS_URL
        )

    def test_users_loader_receives_base_url(self):
        args = self._run_load("users")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL

    def test_completed_works_loader_receives_base_url(self):
        args = self._run_load("completed_works")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL

    def test_payments_loader_receives_base_url(self):
        args = self._run_load("payments")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL

    def test_invoices_loader_receives_base_url(self):
        args = self._run_load("invoices")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL

    def test_assessments_loader_receives_base_url(self):
        args = self._run_load("assessments")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL

    def test_completed_modules_loader_receives_base_url(self):
        args = self._run_load("completed_modules")
        assert args is not None
        assert args.kwargs.get("base_url") == _TEST_LABS_URL

    def test_base_url_none_falls_through_to_default(self):
        """When base_url=None, the loader falls back to settings.CONNECT_API_URL."""
        from mcp_server.services import materializer as mat

        tm = _make_tenant_membership()
        conn = MagicMock()
        conn.autocommit = False

        with patch.object(mat, "ConnectUserLoader") as mock_cls:
            mock_loader = MagicMock()
            mock_loader.load_pages.return_value = iter([])
            mock_cls.return_value = mock_loader
            with patch.object(mat, "_write_connect_users", return_value=0):
                mat._load_connect_source(
                    "users",
                    tm,
                    _CREDENTIAL,
                    "test_schema",
                    conn,
                    base_url=None,
                )
            args = mock_cls.call_args
            assert args is not None
            # base_url=None is passed; ConnectBaseLoader interprets it as
            # "use settings.CONNECT_API_URL" — the correct default behavior.
            assert args.kwargs.get("base_url") is None


class TestConnectLabsDiscoverPhaseBaseUrl:
    """Assert that _run_discover_phase passes base_url for commcare_connect_labs."""

    def _make_pipeline(self, provider: str):
        from mcp_server.pipeline_registry import MetadataDiscoveryConfig, PipelineConfig

        return PipelineConfig(
            name=f"{provider}_sync",
            description="",
            version="1.0",
            provider=provider,
            metadata_discovery=MetadataDiscoveryConfig(),
        )

    def test_discover_phase_passes_labs_base_url(self):
        from mcp_server.services import materializer as mat

        tm = _make_tenant_membership(opp_id="99")
        pipeline = self._make_pipeline("commcare_connect_labs")

        with (
            patch.object(mat, "ConnectMetadataLoader") as mock_cls,
            patch.object(mat, "TenantMetadata"),
            patch("django.conf.settings") as mock_settings,
        ):
            mock_settings.CONNECT_LABS_API_URL = _TEST_LABS_URL
            mock_loader = MagicMock()
            mock_loader.load.return_value = {}
            mock_cls.return_value = mock_loader

            mat._run_discover_phase(tm, _CREDENTIAL, pipeline)

            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs.get("base_url") == _TEST_LABS_URL

    def test_discover_phase_real_connect_has_no_base_url(self):
        """Existing commcare_connect pipelines must not receive a base_url override."""
        from mcp_server.services import materializer as mat

        tm = _make_tenant_membership(opp_id="99")
        pipeline = self._make_pipeline("commcare_connect")

        with (
            patch.object(mat, "ConnectMetadataLoader") as mock_cls,
            patch.object(mat, "TenantMetadata"),
        ):
            mock_loader = MagicMock()
            mock_loader.load.return_value = {}
            mock_cls.return_value = mock_loader

            mat._run_discover_phase(tm, _CREDENTIAL, pipeline)

            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args.kwargs
            # Real Connect path does NOT pass base_url — let the loader use its default
            assert "base_url" not in call_kwargs


class TestConnectLabsSettingPresent:
    """Verify CONNECT_LABS_API_URL is present in Django settings."""

    def test_connect_labs_api_url_setting_exists(self):
        from django.conf import settings

        assert hasattr(settings, "CONNECT_LABS_API_URL")
        # Default is empty string (env var not set in test environment)
        assert isinstance(settings.CONNECT_LABS_API_URL, str)
