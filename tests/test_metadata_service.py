"""Tests for mcp_server/services/metadata.py."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch


def _make_pipeline_config(sources=None, dbt_models=None, relationships=None):
    """Build a minimal PipelineConfig for testing."""
    from mcp_server.pipeline_registry import (
        PipelineConfig,
        RelationshipConfig,
        SourceConfig,
    )

    return PipelineConfig(
        name="commcare_sync",
        description="Test pipeline",
        version="1.0",
        provider="commcare",
        sources=[SourceConfig(name=n, description=d) for n, d in (sources or [])],
        relationships=[RelationshipConfig(**r) for r in (relationships or [])],
    )


def _set_dbt_models(config, models):
    """Attach a TransformConfig with the given model list."""
    from mcp_server.pipeline_registry import TransformConfig

    object.__setattr__(
        config, "transforms", TransformConfig(dbt_project="transforms/commcare", models=models)
    )
    return config


class TestPipelineListTables:
    def test_returns_empty_when_no_completed_run(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(sources=[("cases", "CommCare cases")])

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = None

            result = pipeline_list_tables(mock_ts, pipeline_config)

        assert result == []

    def test_returns_table_entries_from_completed_run(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(
            sources=[("cases", "CommCare case records"), ("forms", "CommCare form records")]
        )

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {
            "sources": {
                "cases": {"rows": 4823},
                "forms": {"rows": 1200},
            }
        }

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = (
                mock_run
            )

            result = pipeline_list_tables(mock_ts, pipeline_config)

        assert len(result) == 2
        cases = next(t for t in result if t["name"] == "cases")
        assert cases["description"] == "CommCare case records"
        assert cases["row_count"] == 4823
        assert cases["materialized_at"] == completed_at.isoformat()
        assert cases["type"] == "table"

    def test_includes_dbt_models_with_null_row_count(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])
        pipeline_config = _set_dbt_models(pipeline_config, ["stg_cases", "stg_forms"])

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"sources": {"cases": {"rows": 100}}}

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = (
                mock_run
            )

            result = pipeline_list_tables(mock_ts, pipeline_config)

        names = [t["name"] for t in result]
        assert "stg_cases" in names
        assert "stg_forms" in names
        stg = next(t for t in result if t["name"] == "stg_cases")
        assert stg["row_count"] is None
        assert stg["materialized_at"] == completed_at.isoformat()


class TestPipelineDescribeTable:
    def _make_ctx(self, schema_name="test_schema"):
        from mcp_server.context import QueryContext

        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            connection_params={},
        )

    def test_returns_none_when_table_not_found(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config()

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}
            result = pipeline_describe_table("nonexistent", ctx, None, pipeline_config)

        assert result is None

    def test_returns_column_structure(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "CommCare case records")])

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [
                    ["case_id", "text", "NO", None],
                    ["case_type", "text", "YES", None],
                ],
                "row_count": 2,
            }
            result = pipeline_describe_table("cases", ctx, None, pipeline_config)

        assert result is not None
        assert result["name"] == "cases"
        assert result["description"] == "CommCare case records"
        assert len(result["columns"]) == 2
        assert result["columns"][0] == {
            "name": "case_id",
            "type": "text",
            "nullable": False,
            "default": None,
            "description": "",
        }

    def test_annotates_properties_column_with_case_types(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])

        mock_tenant_metadata = MagicMock()
        mock_tenant_metadata.metadata = {
            "case_types": [
                {"name": "pregnancy"},
                {"name": "child"},
            ]
        }

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["properties", "jsonb", "YES", "'{}'::jsonb"]],
                "row_count": 1,
            }
            result = pipeline_describe_table("cases", ctx, mock_tenant_metadata, pipeline_config)

        col = result["columns"][0]
        assert "pregnancy" in col["description"]
        assert "child" in col["description"]
        assert col["description"].startswith("Contains case properties")

    def test_annotates_form_data_column_with_form_names(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("forms", "Forms")])

        mock_tenant_metadata = MagicMock()
        mock_tenant_metadata.metadata = {
            "form_definitions": {
                "http://openrosa.org/formdesigner/abc": {"name": "ANC Registration"},
                "http://openrosa.org/formdesigner/xyz": {"name": "Child Visit"},
            }
        }

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["form_data", "jsonb", "YES", "'{}'::jsonb"]],
                "row_count": 1,
            }
            result = pipeline_describe_table("forms", ctx, mock_tenant_metadata, pipeline_config)

        col = result["columns"][0]
        assert "ANC Registration" in col["description"]
        assert "Child Visit" in col["description"]
        assert col["description"].startswith("Contains form submission data")

    def test_graceful_when_tenant_metadata_is_none(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])

        with patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec:
            mock_exec.return_value = {
                "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                "rows": [["properties", "jsonb", "YES", None]],
                "row_count": 1,
            }
            result = pipeline_describe_table("cases", ctx, None, pipeline_config)

        assert result is not None
        assert result["columns"][0]["description"] == ""


class TestPipelineGetMetadata:
    def _make_ctx(self, schema_name="test_schema"):
        from mcp_server.context import QueryContext

        return QueryContext(
            tenant_id="test-domain",
            schema_name=schema_name,
            max_rows_per_query=500,
            max_query_timeout_seconds=30,
            connection_params={},
        )

    def test_returns_empty_when_no_completed_run(self):
        from mcp_server.services.metadata import pipeline_get_metadata

        ctx = self._make_ctx()
        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config()

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = None

            result = pipeline_get_metadata(mock_ts, ctx, None, pipeline_config)

        assert result == {"tables": {}, "relationships": []}

    def test_includes_relationships_from_pipeline_config(self):
        from mcp_server.services.metadata import pipeline_get_metadata

        ctx = self._make_ctx()
        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(
            sources=[("cases", "Cases")],
            relationships=[
                {
                    "from_table": "forms",
                    "from_column": "case_ids",
                    "to_table": "cases",
                    "to_column": "case_id",
                    "description": "Forms reference cases",
                }
            ],
        )

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"sources": {"cases": {"rows": 100}}}

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.metadata._execute_sync_parameterized") as mock_exec,
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.objects.filter.return_value.order_by.return_value.first.return_value = (
                mock_run
            )
            mock_exec.return_value = {
                "rows": [["case_id", "text", "NO", None]],
                "row_count": 1,
            }

            result = pipeline_get_metadata(mock_ts, ctx, None, pipeline_config)

        assert "cases" in result["tables"]
        assert len(result["relationships"]) == 1
        rel = result["relationships"][0]
        assert rel["from_table"] == "forms"
        assert rel["to_table"] == "cases"
        assert rel["description"] == "Forms reference cases"
