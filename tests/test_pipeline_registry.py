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
