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

    def test_source_config_physical_table_name_defaults_to_raw_prefix(self):
        from mcp_server.pipeline_registry import SourceConfig

        s = SourceConfig(name="cases")
        assert s.physical_table_name == "raw_cases"

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

    def test_relationships_defaults_to_empty(self, tmp_path):
        yml = tmp_path / "no_rel.yml"
        yml.write_text(
            "pipeline: no_rel\ndescription: ''\nversion: '1.0'\nprovider: commcare\nsources: []\n"
        )
        registry = PipelineRegistry(pipelines_dir=str(tmp_path))
        config = registry.get("no_rel")
        assert config.relationships == []
