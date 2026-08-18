"""Fitness tests for the staging Cube/semantic deployment seam."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_config(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "config" / name).read_text())


def test_staging_services_share_the_cube_runtime_and_signing_secret():
    for name in (
        "deploy-staging.yml",
        "deploy-staging-worker.yml",
        "deploy-staging-mcp.yml",
    ):
        config = _load_config(name)
        assert config["env"]["clear"]["CUBE_API_URL"] == ("http://scout-staging-cube-web:4000")
        assert "CUBEJS_API_SECRET" in config["env"]["secret"]

    for name in ("deploy-staging.yml", "deploy-staging-worker.yml"):
        clear = _load_config(name)["env"]["clear"]
        assert clear["CUBE_VALIDATOR_URL"] == "http://scout-staging-cube-web:4010"
        assert clear["CUBE_SCHEMA_VALIDATION_REQUIRED"] == "True"


def test_staging_cube_is_internal_and_uses_the_shared_secret():
    config = _load_config("deploy-staging-cube.yml")
    server = config["servers"]["web"]
    assert server["proxy"] is False
    assert server["options"]["network"] == "scout_staging_shared"
    assert server["options"]["network-alias"] == "scout-staging-cube-web"
    assert set(config["env"]["secret"]) == {
        "DATABASE_URL",
        "MANAGED_DATABASE_URL",
        "CUBEJS_API_SECRET",
    }


def test_staging_workflow_builds_and_deploys_cube_before_dependents():
    workflow = (REPO_ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text()
    assert "SCOUT_STAGING_CUBEJS_API_SECRET" in workflow
    assert 'docker build -t "$SCOUT_ECR_REGISTRY/scout/api:$CUBE_TAG" cube_config' in workflow
    assert workflow.index("- name: Deploy Cube") < workflow.index("- name: Deploy MCP")
