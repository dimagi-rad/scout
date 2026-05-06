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
        for attr in ("DISCOVERING", "LOADING", "TRANSFORMING", "COMPLETED", "FAILED", "CANCELLED"):
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
