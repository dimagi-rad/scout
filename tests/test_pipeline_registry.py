from mcp_server.pipeline_registry import PipelineRegistry


class TestPipelineRegistry:
    def test_loads_commcare_sync_pipeline(self, tmp_path):
        yml = tmp_path / "commcare_sync.yml"
        yml.write_text("""
pipeline: commcare_sync
description: "Sync case and form data from CommCare HQ"
version: "1.0"
provider: commcare
sources:
  - name: cases
    description: "CommCare case records"
  - name: forms
    description: "CommCare form submission records"
metadata_discovery:
  description: "Extract application structure"
transforms:
  dbt_project: transforms/commcare
  models:
    - stg_cases
    - stg_forms
""")
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("commcare_sync")
        assert config is not None
        assert config.name == "commcare_sync"
        assert config.description == "Sync case and form data from CommCare HQ"
        assert config.provider == "commcare"
        assert len(config.sources) == 2
        assert config.sources[0].name == "cases"
        assert config.sources[1].name == "forms"
        assert config.has_metadata_discovery is True
        assert config.dbt_models == ["stg_cases", "stg_forms"]

    def test_list_returns_all_pipelines(self, tmp_path):
        (tmp_path / "a.yml").write_text(
            "pipeline: a\ndescription: A\nversion: '1.0'\nprovider: commcare\nsources: []\n"
        )
        (tmp_path / "b.yml").write_text(
            "pipeline: b\ndescription: B\nversion: '1.0'\nprovider: commcare\nsources: []\n"
        )
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        names = [p.name for p in registry.list()]
        assert "a" in names and "b" in names

    def test_get_unknown_pipeline_returns_none(self, tmp_path):
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        assert registry.get("nonexistent") is None

    def test_get_by_provider_returns_matching_pipeline(self, tmp_path):
        (tmp_path / "a.yml").write_text(
            "pipeline: a\ndescription: A\nversion: '1.0'\nprovider: commcare\nsources: []\n"
        )
        (tmp_path / "b.yml").write_text(
            "pipeline: b\ndescription: B\nversion: '1.0'\nprovider: ocs\nsources: []\n"
        )
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        assert registry.get_by_provider("commcare").name == "a"
        assert registry.get_by_provider("ocs").name == "b"
        assert registry.get_by_provider("unknown") is None

    def test_parses_relationships(self, tmp_path):
        yml = tmp_path / "rel.yml"
        yml.write_text("""
pipeline: rel_test
description: "Test"
version: "1.0"
provider: commcare
sources: []
relationships:
  - from_table: forms
    from_column: case_ids
    to_table: cases
    to_column: case_id
    description: "Forms reference cases"
""")
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("rel_test")
        assert len(config.relationships) == 1
        r = config.relationships[0]
        assert r.from_table == "forms"
        assert r.from_column == "case_ids"
        assert r.to_table == "cases"
        assert r.to_column == "case_id"
        assert r.description == "Forms reference cases"

    def test_load_errors_empty_when_all_pipelines_parse(self, tmp_path):
        (tmp_path / "a.yml").write_text(
            "pipeline: a\ndescription: A\nversion: '1.0'\nprovider: commcare\nsources: []\n"
        )
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        assert registry.load_errors == []

    def test_load_errors_records_unparseable_pipeline(self, tmp_path):
        # 07#7: a malformed pipeline YAML must be tracked (a broken deploy), not
        # silently dropped — otherwise the provider just vanishes from the
        # registry and surfaces later as a misleading "No pipeline for provider".
        (tmp_path / "good.yml").write_text(
            "pipeline: good\ndescription: G\nversion: '1.0'\nprovider: ocs\nsources: []\n"
        )
        (tmp_path / "broken.yml").write_text("pipeline: broken\nsources: [: : :\n")  # bad YAML
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))

        # The good pipeline still loads; the broken one is recorded as an error.
        assert registry.get("good") is not None
        assert "broken.yml" in registry.load_errors

    def test_source_config_physical_table_name_defaults_to_raw_prefix(self):
        from mcp_server.pipeline_registry import SourceConfig

        s = SourceConfig(name="cases")
        assert s.physical_table_name == "raw_cases"


class TestNoPipelineErrorMessage:
    """07#7: the 'no pipeline' error must distinguish a misconfigured workspace
    from a broken deploy (pipeline YAML failed to load)."""

    def test_plain_message_when_no_load_errors(self):
        from types import SimpleNamespace

        from apps.workspaces.tasks import _no_pipeline_error

        registry = SimpleNamespace(load_errors=[])
        msg = _no_pipeline_error(registry, "weird_provider")
        assert "weird_provider" in msg
        assert "deploy" not in msg.lower()

    def test_deploy_hint_when_pipeline_yaml_failed_to_load(self):
        from types import SimpleNamespace

        from apps.workspaces.tasks import _no_pipeline_error

        registry = SimpleNamespace(load_errors=["ocs_sync.yml"])
        msg = _no_pipeline_error(registry, "ocs")
        assert "ocs_sync.yml" in msg
        assert "deploy" in msg.lower()

    def test_source_config_physical_table_name_explicit_override(self):
        from mcp_server.pipeline_registry import SourceConfig

        s = SourceConfig(name="cases", table_name="my_cases")
        assert s.physical_table_name == "my_cases"

    def test_loads_connect_sync_pipeline(self):
        """Test that the real connect_sync.yml loads correctly from the pipelines dir."""
        registry = PipelineRegistry()
        config = registry.get("connect_sync")
        assert config is not None
        assert config.name == "connect_sync"
        assert config.provider == "commcare_connect"
        assert len(config.sources) == 7
        source_names = [s.name for s in config.sources]
        assert "visits" in source_names
        assert "users" in source_names
        assert "completed_works" in source_names
        assert "payments" in source_names
        assert "invoices" in source_names
        assert "assessments" in source_names
        assert "completed_modules" in source_names
        assert config.has_metadata_discovery
        assert len(config.relationships) == 5
        rel_from_tables = {r.from_table for r in config.relationships}
        rel_to_tables = {r.to_table for r in config.relationships}
        assert all(t.startswith("raw_") for t in rel_from_tables)
        assert all(t.startswith("raw_") for t in rel_to_tables)

    def test_source_config_resumable_defaults_to_true(self):
        """Issue #187: ``resumable`` defaults to True so most append-mostly
        sources opt in automatically; non-resumable ones set it false."""
        from mcp_server.pipeline_registry import SourceConfig

        assert SourceConfig(name="visits").resumable is True
        assert SourceConfig(name="users", resumable=False).resumable is False

    def test_pipeline_yaml_can_override_resumable(self, tmp_path):
        """The YAML ``resumable: false`` key flows through to SourceConfig."""
        yml = tmp_path / "pipeline.yml"
        yml.write_text("""
pipeline: test_pipeline
description: ""
version: "1.0"
provider: commcare_connect
sources:
  - name: visits
  - name: users
    resumable: false
""")
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("test_pipeline")
        by_name = {s.name: s for s in config.sources}
        assert by_name["visits"].resumable is True
        assert by_name["users"].resumable is False

    def test_connect_sync_resumability_flags(self):
        """Only ``visits`` can keyset-resume: its v2 export carries an integer
        ``id``. ``users`` is mutable (full reload), and the remaining sources'
        v2 export serializers omit ``id`` entirely, so they have no watermark to
        resume from and must be non-resumable (full DROP/CREATE/INSERT)."""
        registry = PipelineRegistry()
        config = registry.get("connect_sync")
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
                f"{src_name} has no per-row id in the v2 export and must be non-resumable"
            )

    def test_progress_unit_defaults_to_rows_and_parses_from_yaml(self, tmp_path):
        """Issue #221: ``progress_unit`` labels the progress counts; OCS
        messages report per-session progress as "sessions"."""
        yml = tmp_path / "pipeline.yml"
        yml.write_text("""
pipeline: test_pipeline
description: ""
version: "1.0"
provider: ocs
sources:
  - name: sessions
  - name: messages
    progress_unit: sessions
""")
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("test_pipeline")
        by_name = {s.name: s for s in config.sources}
        assert by_name["sessions"].progress_unit == "rows"
        assert by_name["messages"].progress_unit == "sessions"

    def test_ocs_sync_messages_progress_unit_is_sessions(self):
        registry = PipelineRegistry()
        config = registry.get("ocs_sync")
        by_name = {s.name: s for s in config.sources}
        assert by_name["messages"].progress_unit == "sessions"

    def test_relationships_defaults_to_empty(self, tmp_path):
        yml = tmp_path / "no_rel.yml"
        yml.write_text(
            "pipeline: no_rel\ndescription: ''\nversion: '1.0'\nprovider: commcare\nsources: []\n"
        )
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("no_rel")
        assert config.relationships == []
