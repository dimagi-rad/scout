"""Tests for the ephemeral dbt project writer (Milestone 5, Task 5.1)."""

import yaml

from apps.transformations.services.dbt_project import write_dbt_project


class FakeAsset:
    """Minimal stand-in for TransformationAsset."""

    def __init__(self, name, sql_content, test_yaml=""):
        self.name = name
        self.sql_content = sql_content
        self.test_yaml = test_yaml


def test_creates_dbt_project_yml(tmp_path):
    assets = [FakeAsset("stg_cases", "SELECT * FROM raw_cases")]
    result = write_dbt_project(tmp_path / "project", "scout_system", assets)

    project_yml = result / "dbt_project.yml"
    assert project_yml.exists()
    config = yaml.safe_load(project_yml.read_text())
    assert config["name"] == "scout_system"
    assert config["profile"] == "data_explorer"
    assert config["config-version"] == 2
    assert config["models"] == {"+materialized": "table"}


def test_creates_one_sql_file_per_asset(tmp_path):
    assets = [
        FakeAsset("stg_cases", "SELECT * FROM raw_cases"),
        FakeAsset("stg_forms", "SELECT * FROM raw_forms"),
    ]
    result = write_dbt_project(tmp_path / "project", "scout_system", assets)

    models_dir = result / "models"
    assert (models_dir / "stg_cases.sql").read_text() == "SELECT * FROM raw_cases"
    assert (models_dir / "stg_forms.sql").read_text() == "SELECT * FROM raw_forms"


def test_merges_test_yaml_into_schema_yml(tmp_path):
    yaml1 = "models:\n  - name: stg_cases\n    columns:\n      - name: case_id\n        tests:\n          - unique\n"
    yaml2 = "models:\n  - name: stg_forms\n    columns:\n      - name: form_id\n        tests:\n          - not_null\n"
    assets = [
        FakeAsset("stg_cases", "SELECT 1", test_yaml=yaml1),
        FakeAsset("stg_forms", "SELECT 2", test_yaml=yaml2),
    ]
    result = write_dbt_project(tmp_path / "project", "scout_test", assets)

    schema_path = result / "models" / "schema.yml"
    assert schema_path.exists()
    schema = yaml.safe_load(schema_path.read_text())
    assert len(schema["models"]) == 2
    names = {m["name"] for m in schema["models"]}
    assert names == {"stg_cases", "stg_forms"}


def test_no_schema_yml_when_no_test_yaml(tmp_path):
    assets = [FakeAsset("stg_cases", "SELECT 1")]
    result = write_dbt_project(tmp_path / "project", "scout_system", assets)

    assert not (result / "models" / "schema.yml").exists()


def test_returns_output_dir(tmp_path):
    assets = [FakeAsset("m1", "SELECT 1")]
    result = write_dbt_project(tmp_path / "project", "test", assets)
    assert result == tmp_path / "project"
