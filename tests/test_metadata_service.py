"""Tests for mcp_server/services/metadata.py."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.services.metadata import _live_tables_in_schema


def _make_pipeline_config(sources=None, relationships=None):
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
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_completed_run(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        pipeline_config = _make_pipeline_config(sources=[("cases", "CommCare cases")])

        with patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls:
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=None)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_table_entries_from_completed_run(self):
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config(
            sources=[("cases", "CommCare case records"), ("forms", "CommCare form records")]
        )

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {
            "sources": {
                "cases": {"state": "completed", "rows": 4823},
                "forms": {"state": "completed", "rows": 1200},
            }
        }

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value={"raw_cases", "raw_forms"}),
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        assert len(result) == 2
        cases = next(t for t in result if t["name"] == "raw_cases")
        assert cases["description"] == "CommCare case records"
        assert cases["materialized_row_count"] == 4823
        assert cases["row_count_verified"] is False
        assert cases["materialized_at"] == completed_at.isoformat()
        assert cases["type"] == "table"
        # Legacy field name must no longer be emitted — the agent must not be
        # able to read it as a verified count.
        assert "row_count" not in cases

    @pytest.mark.asyncio
    async def test_declared_dbt_models_are_not_surfaced(self):
        """Phase 5 (#251): the pipeline dbt-model listing path was removed. Even
        when a config declares ``transforms`` and the models physically exist,
        they must NOT appear in the catalog — only reconciled sources do."""
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])
        pipeline_config = _set_dbt_models(pipeline_config, ["stg_cases", "dim_cases"])

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"sources": {"cases": {"state": "completed", "rows": 100}}}

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value={"raw_cases", "stg_cases", "dim_cases"}),
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        names = [t["name"] for t in result]
        assert names == ["raw_cases"]
        assert "stg_cases" not in names
        assert "dim_cases" not in names

    @pytest.mark.asyncio
    async def test_excludes_failed_and_skipped_sources(self):
        """A PARTIAL run must only surface sources whose state == 'completed'."""
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config(
            sources=[
                ("users", "Connect users"),
                ("visits", "Connect visits"),
                ("completed_works", "Connect completed works"),
                ("payments", "Connect payments"),
            ]
        )

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {
            "sources": {
                "users": {"state": "completed", "rows": 100},
                "visits": {"state": "completed", "rows": 98869},
                "completed_works": {"state": "failed", "rows": 0, "error": "Connect 500"},
                "payments": {"state": "skipped", "rows": 0},
            }
        }

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value={"raw_users", "raw_visits"}),
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        names = {t["name"] for t in result}
        assert names == {"raw_users", "raw_visits"}, (
            "Failed/skipped sources must not appear in the catalog"
        )

    @pytest.mark.asyncio
    async def test_excludes_dropped_physical_tables(self):
        """COMPLETED run, but the physical table is gone — exclude it.
        This is the "ghost catalog after teardown" defect.
        """
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {"sources": {"cases": {"state": "completed", "rows": 100}}}

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value=set()),  # schema was torn down
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        assert result == [], (
            "Catalog must be empty when the schema has been dropped, "
            "even if the COMPLETED run record still exists"
        )

    @pytest.mark.asyncio
    async def test_partial_run_surfaces_committed_sources(self):
        """A PARTIAL run is queryable for its committed sources."""
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config(sources=[("users", "Users"), ("visits", "Visits")])

        completed_at = datetime(2026, 2, 24, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {
            "sources": {
                "users": {"state": "completed", "rows": 100},
                "visits": {"state": "failed", "rows": 0, "error": "Connect 500"},
            }
        }
        # PARTIAL run: ensure the metadata service looks at it (not just COMPLETED).
        mock_run.state = "partial"

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value={"raw_users"}),
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        # The filter passed state__in [COMPLETED, PARTIAL]
        filter_call = mock_run_cls.objects.filter.call_args.kwargs
        assert "state__in" in filter_call
        assert "partial" in filter_call["state__in"]
        # Result has only the completed source.
        names = {t["name"] for t in result}
        assert names == {"raw_users"}

    @pytest.mark.asyncio
    async def test_in_progress_source_not_listed(self):
        """Issue #187: a source mid-resume (state="in_progress") must be
        excluded from the catalog even if its physical table partly exists.
        The table is only partially populated; surfacing it would let the
        agent query incomplete data and produce wrong answers.
        """
        from mcp_server.services.metadata import pipeline_list_tables

        mock_ts = MagicMock()
        mock_ts.schema_name = "t_test"
        pipeline_config = _make_pipeline_config(
            sources=[
                ("users", "Connect users"),
                ("completed_works", "Connect completed works"),
            ]
        )

        completed_at = datetime(2026, 5, 27, 10, 0, 0, tzinfo=UTC)
        mock_run = MagicMock()
        mock_run.completed_at = completed_at
        mock_run.result = {
            "sources": {
                "users": {"state": "completed", "rows": 100},
                "completed_works": {
                    "state": "in_progress",
                    "rows": 4700,
                    "cursor_state": {
                        "last_id": 1500,
                        "last_committed_at": "2026-05-27T09:00:00Z",
                    },
                },
            }
        }
        mock_run.state = "partial"

        with (
            patch("mcp_server.services.metadata.MaterializationRun") as mock_run_cls,
            patch(
                "mcp_server.services.metadata._live_tables_in_schema",
                AsyncMock(return_value={"raw_users", "raw_completed_works"}),
            ),
        ):
            mock_run_cls.RunState.COMPLETED = "completed"
            mock_run_cls.RunState.PARTIAL = "partial"
            qs = mock_run_cls.objects.filter.return_value.order_by.return_value
            qs.afirst = AsyncMock(return_value=mock_run)

            result = await pipeline_list_tables(mock_ts, pipeline_config)

        names = {t["name"] for t in result}
        assert names == {"raw_users"}, (
            "in_progress source must not appear in the catalog, even if its "
            "physical table partially exists"
        )


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

    @pytest.mark.asyncio
    async def test_returns_none_when_table_not_found(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config()

        with patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0}),
        ):
            result = await pipeline_describe_table("nonexistent", ctx, None, pipeline_config)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_column_structure(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "CommCare case records")])

        with patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(
                return_value={
                    "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                    "rows": [
                        ["case_id", "text", "NO", None],
                        ["case_type", "text", "YES", None],
                    ],
                    "row_count": 2,
                }
            ),
        ):
            result = await pipeline_describe_table("raw_cases", ctx, None, pipeline_config)

        assert result is not None
        assert result["name"] == "raw_cases"
        assert result["description"] == "CommCare case records"
        assert len(result["columns"]) == 2
        assert result["columns"][0] == {
            "name": "case_id",
            "type": "text",
            "nullable": False,
            "default": None,
            "description": "",
        }

    @pytest.mark.asyncio
    async def test_annotates_properties_column_with_case_types(self):
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

        with patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(
                return_value={
                    "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                    "rows": [["properties", "jsonb", "YES", "'{}'::jsonb"]],
                    "row_count": 1,
                }
            ),
        ):
            result = await pipeline_describe_table(
                "raw_cases", ctx, mock_tenant_metadata, pipeline_config
            )

        col = result["columns"][0]
        assert "pregnancy" in col["description"]
        assert "child" in col["description"]
        assert col["description"].startswith("Contains case properties")

    @pytest.mark.asyncio
    async def test_annotates_form_data_column_with_form_names(self):
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

        with patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(
                return_value={
                    "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                    "rows": [["form_data", "jsonb", "YES", "'{}'::jsonb"]],
                    "row_count": 1,
                }
            ),
        ):
            result = await pipeline_describe_table(
                "raw_forms", ctx, mock_tenant_metadata, pipeline_config
            )

        col = result["columns"][0]
        assert "ANC Registration" in col["description"]
        assert "Child Visit" in col["description"]
        assert col["description"].startswith("Contains form submission data")

    @pytest.mark.asyncio
    async def test_graceful_when_tenant_metadata_is_none(self):
        from mcp_server.services.metadata import pipeline_describe_table

        ctx = self._make_ctx()
        pipeline_config = _make_pipeline_config(sources=[("cases", "Cases")])

        with patch(
            "mcp_server.services.metadata._execute_async_parameterized",
            new=AsyncMock(
                return_value={
                    "columns": ["column_name", "data_type", "is_nullable", "column_default"],
                    "rows": [["properties", "jsonb", "YES", None]],
                    "row_count": 1,
                }
            ),
        ):
            result = await pipeline_describe_table("raw_cases", ctx, None, pipeline_config)

        assert result is not None
        assert result["columns"][0]["description"] == ""


class TestLiveTablesInSchema:
    """Exercise the connection_params plumbing, not just the mocked result.

    Regression guard: _live_tables_in_schema previously built a QueryContext
    with connection_params={}, which expands to a parameterless
    psycopg.AsyncConnection.connect() that falls back to (unset) libpq env-var
    defaults in the MCP container — so every healthy run surfaced zero tables.
    These tests assert the ctx handed to the executor carries real host/db/user
    connection params derived from MANAGED_DATABASE_URL.
    """

    MANAGED_URL = "postgresql://scout_user:secret@db.internal:5432/scout"

    @pytest.mark.asyncio
    async def test_passes_populated_connection_params_to_executor(self):
        captured = {}

        async def fake_exec(ctx, sql, params, _timeout):
            captured["ctx"] = ctx
            return {"rows": [["raw_cases"], ["raw_forms"]]}

        with (
            patch("mcp_server.services.metadata.settings.MANAGED_DATABASE_URL", self.MANAGED_URL),
            patch(
                "mcp_server.services.metadata._execute_async_parameterized",
                new=AsyncMock(side_effect=fake_exec),
            ),
        ):
            result = await _live_tables_in_schema("t_acme")

        assert result == {"raw_cases", "raw_forms"}
        ctx = captured["ctx"]
        # The defect was an empty connection_params dict; assert it is populated
        # with the libpq fields parsed from MANAGED_DATABASE_URL.
        assert ctx.connection_params, "connection_params must not be empty"
        assert ctx.connection_params["host"] == "db.internal"
        assert ctx.connection_params["dbname"] == "scout"
        assert ctx.connection_params["user"] == "scout_user"
        assert ctx.connection_params["password"] == "secret"
        assert "search_path=t_acme" in ctx.connection_params["options"]

    @pytest.mark.asyncio
    async def test_returns_empty_set_when_managed_url_unset(self):
        with patch("mcp_server.services.metadata.settings.MANAGED_DATABASE_URL", ""):
            result = await _live_tables_in_schema("t_acme")
        assert result == set()
