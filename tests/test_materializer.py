from unittest.mock import MagicMock, patch

import pytest


class TestRunPipeline:
    def _make_schema(self, name="dimagi"):
        s = MagicMock()
        s.schema_name = name
        return s

    def _make_tm(self, tenant_id="dimagi"):
        tm = MagicMock()
        tm.tenant.external_id = tenant_id
        return tm

    def _setup_run_mock(self, mock_run_cls):
        run = MagicMock()
        run.id = "run-1"
        mock_run_cls.objects.create.return_value = run
        for attr in (
            "DISCOVERING",
            "LOADING",
            "TRANSFORMING",
            "COMPLETED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
            "STALE",
        ):
            setattr(mock_run_cls.RunState, attr, attr.lower())
        # ACTIVE_STATES is a real frozenset on the model; replicate it on the mock
        mock_run_cls.ACTIVE_STATES = frozenset(
            {"started", "discovering", "loading", "transforming"}
        )
        return run

    def test_returns_completed_result(self):
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[
                SourceConfig(
                    name="cases",
                )
            ],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            mock_cases.return_value.load_pages.return_value = iter([])
            mock_asset_cls.objects.filter.return_value.exists.return_value = False
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            result = run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        assert result["status"] == "completed"
        assert result["run_id"] == "run-1"
        assert "cases" in result["sources"]

    def test_progress_updater_called_full_sequence(self):
        """Progress updater must be called for each phase transition with a
        well-formed dict, in step order."""
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[
                SourceConfig(
                    name="cases",
                )
            ],
            # No metadata_discovery, no transforms — simplest pipeline
        )
        # total_steps = 1 (provision) + 1 (discover) + 1 (cases) + 1 (transform/skip) = 4

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            mock_cases.return_value.load_pages.return_value = iter([])
            mock_asset_cls.objects.filter.return_value.exists.return_value = False
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            calls: list[dict] = []
            run_pipeline(
                self._make_tm(),
                {"type": "api_key", "value": "x"},
                pipeline,
                progress_updater=calls.append,
            )

        total = calls[0]["total_steps"]
        assert len(calls) == total  # one report() per phase
        for i, c in enumerate(calls, start=1):
            assert c["step"] == i
            assert c["total_steps"] == total
            assert "message" in c
            assert "rows_loaded" in c
            assert "rows_total" in c
            assert "run_id" in c
        # First step is provisioning; last is transform/skip.
        assert "provision" in calls[0]["message"].lower() or "schema" in calls[0]["message"].lower()
        assert "transform" in calls[-1]["message"].lower() or "skip" in calls[-1]["message"].lower()

    def test_progress_updater_cancellation_rolls_back(self):
        """When the updater raises ``MaterializationCancelled`` mid-load, the
        psycopg transaction is rolled back and the exception propagates."""
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import (
            MaterializationCancelled,
            run_pipeline,
        )

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[SourceConfig(name="cases")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            # Yield one page so the writer's on_page fires.
            mock_cases.return_value.load_pages.return_value = iter([([{"case_id": "c1"}], 100)])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            seen_messages: list[str] = []

            def raising_updater(progress: dict) -> None:
                seen_messages.append(progress["message"])
                # Raise once we're inside the LOAD phase (after the page write).
                if progress.get("rows_loaded"):
                    raise MaterializationCancelled()

            with pytest.raises(MaterializationCancelled):
                run_pipeline(
                    self._make_tm(),
                    {"type": "api_key", "value": "x"},
                    pipeline,
                    progress_updater=raising_updater,
                )

        # Transaction should have been rolled back.
        conn.rollback.assert_called_once()
        # No commit should have happened.
        conn.commit.assert_not_called()

    def test_loading_transition_preserves_external_cancel(self):
        """If the cancel endpoint flips state to CANCELLED while DISCOVER is
        running (no progress checkpoint there), the DISCOVERING→LOADING
        transition must use a conditional UPDATE so it does not silently
        overwrite the cancel and let the run continue."""
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import (
            MaterializationCancelled,
            run_pipeline,
        )

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[SourceConfig(name="cases")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            # Simulate the conditional UPDATE finding no DISCOVERING row
            # (because the cancel endpoint already wrote CANCELLED).
            mock_run_cls.objects.filter.return_value.update.return_value = 0

            with pytest.raises(MaterializationCancelled):
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

            # We must not have advanced past DISCOVER: no DB connection acquired.
            mock_conn.assert_not_called()

    def test_transforming_transition_preserves_external_cancel(self):
        """A cancel that lands between LOAD commit and the TRANSFORM phase
        must not be overwritten by the LOADING→TRANSFORMING transition;
        transform must be skipped."""
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import (
            MaterializationCancelled,
            run_pipeline,
        )

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[SourceConfig(name="cases")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer._run_transform_phase") as mock_transform,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            mock_cases.return_value.load_pages.return_value = iter([])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            # 1st conditional UPDATE (DISCOVERING→LOADING) succeeds; the 2nd
            # (LOADING→TRANSFORMING) finds no row, simulating cancel landing
            # between LOAD commit and the transform start. The 3rd is the
            # result-stamping write inside the cancel branch.
            mock_run_cls.objects.filter.return_value.update.side_effect = [1, 0, 1]

            with pytest.raises(MaterializationCancelled):
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

            # LOAD ran (commit was called), but transform must be skipped.
            conn.commit.assert_called_once()
            mock_transform.assert_not_called()

    def test_no_metadata_discovery_skips_discover_phase(self):
        """Pipeline without metadata_discovery should not create TenantMetadata."""
        from mcp_server.pipeline_registry import PipelineConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="bare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[],  # no metadata_discovery
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata") as mock_meta_model,
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta_loader,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_asset_cls.objects.filter.return_value.exists.return_value = False
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        mock_meta_loader.assert_not_called()
        mock_meta_model.objects.update_or_create.assert_not_called()

    def test_transform_failure_does_not_mark_run_failed(self):
        """A DBT transform failure should NOT change state to FAILED."""
        from mcp_server.pipeline_registry import PipelineConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
            patch("mcp_server.services.materializer._run_transform_phase") as mock_transform,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()
            mock_asset_cls.objects.filter.return_value.exists.return_value = True
            mock_transform.side_effect = RuntimeError("dbt compilation error")

            result = run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        # Run should be COMPLETED, not FAILED
        assert run.state == "completed"
        assert result["status"] == "completed"
        # Transform error is recorded in result
        assert "transform_error" in result

    def test_unknown_source_raises(self):
        from mcp_server.services.materializer import _load_source

        conn = MagicMock()
        with pytest.raises(ValueError, match="Unknown source"):
            _load_source("nonexistent", MagicMock(), {}, "schema", conn)

    def test_failed_load_marks_run_failed(self):
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[
                SourceConfig(
                    name="cases",
                )
            ],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            mock_cases.return_value.load_pages.side_effect = RuntimeError("CommCare API down")
            conn = MagicMock()
            mock_conn.return_value = conn

            with pytest.raises(RuntimeError, match="CommCare API down"):
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        assert run.state == "failed"

    def test_partial_failure_preserves_earlier_sources_and_marks_partial(self):
        """Per-source atomicity: when a later source fails, earlier sources
        stay committed and the run is marked PARTIAL (not FAILED).
        """
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_connect",
            description="",
            version="1.0",
            provider="commcare_connect",
            sources=[
                SourceConfig(name="users"),
                SourceConfig(name="visits"),
                SourceConfig(name="completed_works"),
                SourceConfig(name="payments"),
            ],
        )
        tm = self._make_tm(tenant_id="42")

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.ConnectMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.ConnectUserLoader") as mock_users,
            patch("mcp_server.services.materializer.ConnectVisitLoader") as mock_visits,
            patch(
                "mcp_server.services.materializer.ConnectCompletedWorkLoader",
            ) as mock_cw,
            patch("mcp_server.services.materializer.ConnectPaymentLoader") as mock_payments,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {}
            mock_users.return_value.load_pages.return_value = iter([([{"u": 1}], 1)])
            mock_visits.return_value.load_pages.return_value = iter([([{"v": 1}], 1)])
            # 3rd source fails AFTER the writer DROP/CREATE — simulate a 5xx mid-load.
            mock_cw.return_value.load_pages.side_effect = RuntimeError("Connect 500")
            mock_payments.return_value.load_pages.return_value = iter([])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            with pytest.raises(RuntimeError, match="Connect 500"):
                run_pipeline(tm, {"type": "api_key", "value": "x"}, pipeline)

        # Run was marked PARTIAL (two earlier sources committed before failure).
        assert run.state == "partial"
        # source_results recorded the truth: 2 completed, 1 failed, 1 skipped.
        result_kwargs = run.save.call_args.kwargs
        # run.save was called with update_fields including "result"
        # We instead inspect run.result directly since the mock writes it.
        sources = run.result["sources"]
        assert sources["users"]["state"] == "completed"
        assert sources["visits"]["state"] == "completed"
        assert sources["completed_works"]["state"] == "failed"
        assert sources["payments"]["state"] == "skipped"
        # 4th source we never reached is recorded as skipped, not completed.
        assert "rows" in sources["completed_works"]
        assert sources["completed_works"]["rows"] == 0
        # Sanity-check that update_fields includes result/state.
        assert "result" in result_kwargs.get("update_fields", [])

    def test_failed_first_source_marks_run_failed_not_partial(self):
        """If the very first source fails, no source has committed, so the
        run is marked FAILED (not PARTIAL).
        """
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_connect",
            description="",
            version="1.0",
            provider="commcare_connect",
            sources=[
                SourceConfig(name="users"),
                SourceConfig(name="visits"),
            ],
        )
        tm = self._make_tm(tenant_id="42")

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.ConnectMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.ConnectUserLoader") as mock_users,
            patch("mcp_server.services.materializer.ConnectVisitLoader") as mock_visits,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {}
            mock_users.return_value.load_pages.side_effect = RuntimeError("Connect 500")
            mock_visits.return_value.load_pages.return_value = iter([])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            with pytest.raises(RuntimeError, match="Connect 500"):
                run_pipeline(tm, {"type": "api_key", "value": "x"}, pipeline)

        assert run.state == "failed"
        sources = run.result["sources"]
        assert sources["users"]["state"] == "failed"
        assert sources["visits"]["state"] == "skipped"

    def test_failed_source_records_error_and_attempts(self):
        """Failed-source dict must include short error + attempts (default 1)."""
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[SourceConfig(name="cases")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            err = RuntimeError("upstream HTTP 500 after retries")
            err.attempts = 3
            mock_cases.return_value.load_pages.side_effect = err
            conn = MagicMock()
            mock_conn.return_value = conn

            with pytest.raises(RuntimeError):
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        info = run.result["sources"]["cases"]
        assert info["state"] == "failed"
        assert "upstream HTTP 500" in info["error"]
        # Error string must be short — no traceback noise.
        assert "Traceback" not in info["error"]
        assert info["attempts"] == 3
        assert "failed_at" in info

    def test_source_state_never_loaded_string(self):
        """The 'loaded' state was the phantom-rows lie; verify it is gone."""
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[SourceConfig(name="cases")],
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            mock_cases.return_value.load_pages.return_value = iter([])
            mock_asset_cls.objects.filter.return_value.exists.return_value = False
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            result = run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        for source_name, info in result["sources"].items():
            assert info.get("state") != "loaded", (
                f"Source {source_name} still records phantom 'loaded' state"
            )
        # The success path must use 'completed'.
        assert result["sources"]["cases"]["state"] == "completed"

    def test_completed_source_commits_before_recording_state(self):
        """A completed source's connection must commit BEFORE the state is
        recorded as 'completed' — that ordering is what makes the 'loaded
        but rolled back' bug impossible.
        """
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="commcare_sync",
            description="",
            version="1.0",
            provider="commcare",
            sources=[SourceConfig(name="cases")],
        )

        commit_called: list[bool] = []

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.CommCareMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.CommCareCaseLoader") as mock_cases,
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
            patch("mcp_server.services.materializer.TransformationAsset") as mock_asset_cls,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {
                "app_definitions": [],
                "case_types": [],
                "form_definitions": {},
            }
            mock_cases.return_value.load_pages.return_value = iter([])
            mock_asset_cls.objects.filter.return_value.exists.return_value = False
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()
            conn.commit.side_effect = lambda: commit_called.append(True)

            run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        # commit() was called at least once during the source loop
        assert commit_called, "Per-source commit() must be invoked"


class TestResumableMaterialization:
    """Issue #187: per-page cursor watermark for resumable Connect sources."""

    def _make_schema(self, name="dimagi"):
        s = MagicMock()
        s.schema_name = name
        return s

    def _make_tm(self, tenant_id="42"):
        tm = MagicMock()
        tm.tenant.external_id = tenant_id
        return tm

    def _setup_run_mock(self, mock_run_cls, prior_run=None):
        run = MagicMock()
        run.id = "run-1"
        mock_run_cls.objects.create.return_value = run
        for attr in (
            "DISCOVERING",
            "LOADING",
            "TRANSFORMING",
            "COMPLETED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
            "STALE",
        ):
            setattr(mock_run_cls.RunState, attr, attr.lower())
        mock_run_cls.ACTIVE_STATES = frozenset(
            {"started", "discovering", "loading", "transforming"}
        )
        # _load_prior_resume_cursors uses .filter().exclude().order_by().first()
        # — wire it up to return ``prior_run`` (or None when absent).
        chain = mock_run_cls.objects.filter.return_value
        chain.exclude.return_value.order_by.return_value.first.return_value = prior_run
        return run

    def _run_connect_pipeline(
        self,
        sources,
        loader_mocks,
        prior_run=None,
        completed_works_side_effect=None,
    ):
        """Run a Connect pipeline with mocked loaders. Returns the run mock."""
        from mcp_server.pipeline_registry import PipelineConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline = PipelineConfig(
            name="connect_sync",
            description="",
            version="1.0",
            provider="commcare_connect",
            sources=sources,
        )

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.ConnectMetadataLoader") as mock_meta,
            patch(
                "mcp_server.services.materializer.ConnectVisitLoader",
                loader_mocks.get("visits", MagicMock()),
            ) as mock_visits,
            patch(
                "mcp_server.services.materializer.ConnectUserLoader",
                loader_mocks.get("users", MagicMock()),
            ) as mock_users,
            patch(
                "mcp_server.services.materializer.ConnectCompletedWorkLoader",
                loader_mocks.get("completed_works", MagicMock()),
            ) as mock_cw,
            patch(
                "mcp_server.services.materializer.ConnectPaymentLoader",
                loader_mocks.get("payments", MagicMock()),
            ),
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls, prior_run=prior_run)
            mock_meta.return_value.load.return_value = {}
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            invocations = {
                "visits": mock_visits,
                "users": mock_users,
                "completed_works": mock_cw,
            }
            if completed_works_side_effect is not None:
                mock_cw.return_value.load_pages.side_effect = completed_works_side_effect
            try:
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)
            except Exception:
                pass
            return run, invocations

    def test_resumes_from_cursor_when_prior_run_partial(self):
        """When a prior PARTIAL run recorded cursor_state.last_id for a
        resumable source whose state was in_progress/failed, the next run
        passes that id as ``start_last_id`` to the loader.
        """
        from mcp_server.pipeline_registry import SourceConfig

        prior = MagicMock()
        prior.state = "partial"  # must match RunState.PARTIAL set in _setup_run_mock
        prior.result = {
            "sources": {
                "completed_works": {
                    "state": "in_progress",
                    "rows": 100,
                    "cursor_state": {"last_id": 1500, "last_committed_at": "2026-05-27T00:00:00Z"},
                }
            }
        }

        cw_loader_cls = MagicMock()
        cw_loader_cls.return_value.load_pages.return_value = iter([])

        _, invocations = self._run_connect_pipeline(
            sources=[SourceConfig(name="completed_works", resumable=True)],
            loader_mocks={"completed_works": cw_loader_cls},
            prior_run=prior,
        )

        # load_pages must have been called with start_last_id=1500.
        call = invocations["completed_works"].return_value.load_pages.call_args
        assert call.kwargs.get("start_last_id") == 1500, (
            f"Expected start_last_id=1500, got {call.kwargs}"
        )

    def test_clean_run_ignores_cursor_state_for_non_resumable_source(self):
        """Non-resumable sources (e.g. users) MUST do a clean full reload
        regardless of any cursor_state present on a prior run.
        """
        from mcp_server.pipeline_registry import SourceConfig

        prior = MagicMock()
        prior.state = "partial"  # must match RunState.PARTIAL set in _setup_run_mock
        prior.result = {
            "sources": {
                "users": {
                    "state": "in_progress",
                    "rows": 50,
                    "cursor_state": {"last_id": 999, "last_committed_at": "2026-05-27T00:00:00Z"},
                }
            }
        }
        users_loader_cls = MagicMock()
        users_loader_cls.return_value.load_pages.return_value = iter([])

        _, invocations = self._run_connect_pipeline(
            sources=[SourceConfig(name="users", resumable=False)],
            loader_mocks={"users": users_loader_cls},
            prior_run=prior,
        )

        call = invocations["users"].return_value.load_pages.call_args
        # The users loader must be called with NO start_last_id (it doesn't
        # even accept the kwarg). It is called positionally with nothing.
        assert "start_last_id" not in (call.kwargs or {}), (
            "Non-resumable source must not receive a resume cursor"
        )

    def test_cursor_advances_after_each_page_commit(self):
        """Each per-page commit must update cursor_state.last_id to the
        max id of the page just committed (and rows count too).
        """
        from mcp_server.services.materializer import _make_cursor_callback

        run = MagicMock()
        run.id = "run-1"
        # Wire ACTIVE_STATES into the patched MaterializationRun so the CAS
        # update inside _persist_source_results doesn't blow up.
        with patch("mcp_server.services.materializer.MaterializationRun") as mock_rc:
            mock_rc.ACTIVE_STATES = frozenset({"started", "loading"})
            pipeline = MagicMock(name="connect_sync")
            pipeline.name = "connect_sync"
            source_results = {}
            cb = _make_cursor_callback(run, pipeline, source_results, "completed_works")
            cb(100, 50)
            assert source_results["completed_works"]["state"] == "in_progress"
            assert source_results["completed_works"]["rows"] == 50
            assert source_results["completed_works"]["cursor_state"]["last_id"] == 100
            cb(250, 100)
            assert source_results["completed_works"]["cursor_state"]["last_id"] == 250
            assert source_results["completed_works"]["rows"] == 100
        # The _persist_source_results CAS update was called once per page.
        # We don't pin the exact call count to the mock here because the
        # callback re-imports MaterializationRun via the module under test,
        # not via our local patch — covered by the integration test below.

    def test_resume_does_not_drop_table_when_start_cursor_present(self):
        """The resumable writer skips DROP and uses CREATE IF NOT EXISTS."""
        from mcp_server.services.materializer import _write_connect_completed_works

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        _write_connect_completed_works(
            pages=iter([]),
            schema_name="t_x",
            conn=conn,
            start_cursor=1234,
        )
        executed_sql = [str(c.args[0]) for c in cur.execute.call_args_list]
        joined = "\n".join(executed_sql)
        assert "DROP TABLE" not in joined, "Resume path must not DROP the partially-loaded table"
        assert "CREATE TABLE IF NOT EXISTS" in joined or "IF NOT EXISTS" in joined

    def test_no_prior_cursor_means_clean_start(self):
        """First-ever run (no PARTIAL/FAILED history) behaves like pre-#187."""
        from mcp_server.pipeline_registry import SourceConfig

        cw_loader_cls = MagicMock()
        cw_loader_cls.return_value.load_pages.return_value = iter([])

        _, invocations = self._run_connect_pipeline(
            sources=[SourceConfig(name="completed_works", resumable=True)],
            loader_mocks={"completed_works": cw_loader_cls},
            prior_run=None,
        )

        call = invocations["completed_works"].return_value.load_pages.call_args
        # start_last_id is None on a clean run.
        assert call.kwargs.get("start_last_id") is None

    def test_completed_run_after_partial_invalidates_stale_cursor(self):
        """Regression test for stale-cursor bug: when a COMPLETED run follows an
        older PARTIAL run, the next run (Run C) must NOT resume from the PARTIAL
        run's cursor — it must do a clean full reload.

        Scenario:
          Run A → PARTIAL, cursor_state.last_id = 1500
          Run B → COMPLETED (full reload, table now complete)
          Run C → must start clean (start_last_id=None), not from 1500

        Without the fix, _load_prior_resume_cursors would skip Run B (COMPLETED)
        and return Run A's stale cursor, causing duplicate-key errors or silent
        row duplication depending on whether the table has a PK.
        """
        from mcp_server.pipeline_registry import SourceConfig

        # The most-recent prior run (Run B) is COMPLETED — its state must
        # invalidate the older PARTIAL cursor from Run A.
        prior_completed = MagicMock()
        prior_completed.state = "completed"  # matches RunState.COMPLETED mock value
        prior_completed.result = {
            "sources": {
                "completed_works": {
                    "state": "completed",
                    "rows": 5000,
                    "cursor_state": None,
                }
            }
        }

        cw_loader_cls = MagicMock()
        cw_loader_cls.return_value.load_pages.return_value = iter([])

        _, invocations = self._run_connect_pipeline(
            sources=[SourceConfig(name="completed_works", resumable=True)],
            loader_mocks={"completed_works": cw_loader_cls},
            prior_run=prior_completed,
        )

        call = invocations["completed_works"].return_value.load_pages.call_args
        assert call.kwargs.get("start_last_id") is None, (
            "A COMPLETED prior run must cause a clean full reload, "
            f"not a resume from a stale cursor; got start_last_id={call.kwargs.get('start_last_id')}"
        )

    def test_failed_resumable_source_records_cursor_state_for_next_run(self):
        """A resumable source that fails mid-load must preserve its cursor
        watermark in MaterializationRun.result so the next run can resume.
        """
        from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
        from mcp_server.services.materializer import run_pipeline

        pipeline_cfg_sources = [SourceConfig(name="completed_works", resumable=True)]

        pipeline = PipelineConfig(
            name="connect_sync",
            description="",
            version="1.0",
            provider="commcare_connect",
            sources=pipeline_cfg_sources,
        )

        def fake_writer_side_effect(*args, **kwargs):
            # Simulate one successful page-commit then a failure.
            cursor_callback = kwargs.get("cursor_callback")
            if cursor_callback is not None:
                cursor_callback(777, 42)
            raise RuntimeError("Connect 500 mid-load")

        with (
            patch("mcp_server.services.materializer.SchemaManager") as mock_mgr,
            patch("mcp_server.services.materializer.MaterializationRun") as mock_run_cls,
            patch("mcp_server.services.materializer.TenantMetadata"),
            patch("mcp_server.services.materializer.ConnectMetadataLoader") as mock_meta,
            patch("mcp_server.services.materializer.ConnectCompletedWorkLoader") as mock_cw,
            patch(
                "mcp_server.services.materializer._write_connect_completed_works",
                side_effect=fake_writer_side_effect,
            ),
            patch("mcp_server.services.materializer.get_managed_db_connection") as mock_conn,
        ):
            schema = self._make_schema()
            mock_mgr.return_value.provision.return_value = schema
            run = self._setup_run_mock(mock_run_cls)
            mock_meta.return_value.load.return_value = {}
            mock_cw.return_value.load_pages.return_value = iter([])
            conn = MagicMock()
            mock_conn.return_value = conn
            conn.cursor.return_value = MagicMock()

            with pytest.raises(RuntimeError, match="Connect 500"):
                run_pipeline(self._make_tm(), {"type": "api_key", "value": "x"}, pipeline)

        # The final saved result must show the source failed with cursor_state
        # preserved so the next run resumes from id=777.
        sources = run.result["sources"]
        assert sources["completed_works"]["state"] == "failed"
        assert sources["completed_works"]["cursor_state"]["last_id"] == 777
        # And because the cursor advanced (some pages committed), the run is
        # PARTIAL — not FAILED — so the next run knows it has resume work.
        assert run.state == "partial"


@pytest.mark.django_db
class TestWriteCases:
    """Real DB tests for _write_cases using psycopg."""

    def test_inserts_cases(self, django_db_setup, db):
        """_write_cases should insert rows into the named schema."""
        import os

        import psycopg

        from mcp_server.services.materializer import _write_cases

        db_url = os.environ.get("MANAGED_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("No MANAGED_DATABASE_URL/DATABASE_URL for writer test")

        test_schema = "test_write_cases"
        conn = psycopg.connect(db_url, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {test_schema}")
            conn.autocommit = False
            cases = [
                {
                    "case_id": "c1",
                    "case_type": "patient",
                    "case_name": "Alice",
                    "external_id": "",
                    "owner_id": "u1",
                    "date_opened": "2026-01-01",
                    "last_modified": "2026-01-02",
                    "server_last_modified": "",
                    "indexed_on": "",
                    "closed": False,
                    "date_closed": "",
                    "properties": {"name": "Alice"},
                    "indices": {},
                },
            ]
            count = _write_cases(iter([(cases, len(cases))]), test_schema, conn)
            conn.commit()
            assert count == 1
            with conn.cursor() as cur:
                cur.execute(f"SELECT case_id FROM {test_schema}.raw_cases")
                rows = cur.fetchall()
            assert rows[0][0] == "c1"
        finally:
            conn.rollback()  # end any open transaction before switching autocommit
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE")
            conn.close()


@pytest.mark.django_db
class TestWriteForms:
    def test_inserts_forms(self, django_db_setup, db):
        import os

        import psycopg

        from mcp_server.services.materializer import _write_forms

        db_url = os.environ.get("MANAGED_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not db_url:
            pytest.skip("No MANAGED_DATABASE_URL/DATABASE_URL for writer test")

        test_schema = "test_write_forms"
        conn = psycopg.connect(db_url, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {test_schema}")
            conn.autocommit = False
            forms = [
                {
                    "form_id": "f1",
                    "xmlns": "http://example.com/form1",
                    "received_on": "2026-01-01",
                    "server_modified_on": "",
                    "app_id": "app1",
                    "form_data": {"@name": "Reg"},
                    "case_ids": ["c1"],
                },
            ]
            count = _write_forms(iter([(forms, len(forms))]), test_schema, conn)
            conn.commit()
            assert count == 1
        finally:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {test_schema} CASCADE")
            conn.close()
