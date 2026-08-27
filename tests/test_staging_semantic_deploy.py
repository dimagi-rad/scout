"""Fitness tests for the staging Cube/semantic deployment seam."""

from pathlib import Path

from tests.kamal_config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _staging(name: str) -> dict:
    return load_config(name, destination="staging")


def test_staging_services_share_the_cube_runtime_and_signing_secret():
    for name in ("deploy.yml", "deploy-worker.yml", "deploy-mcp.yml"):
        config = _staging(name)
        assert config["env"]["clear"]["CUBE_API_URL"] == ("http://scout-staging-cube-web:4000")
        assert "CUBEJS_API_SECRET" in config["env"]["secret"]

    for name in ("deploy.yml", "deploy-worker.yml"):
        clear = _staging(name)["env"]["clear"]
        assert clear["CUBE_VALIDATOR_URL"] == "http://scout-staging-cube-web:4010"
        assert clear["CUBE_SCHEMA_VALIDATION_REQUIRED"] == "True"


def test_staging_cube_is_internal_and_uses_the_shared_secret():
    config = _staging("deploy-cube.yml")
    assert config["image"] == "scout/mcp"
    assert config["builder"]["context"] == "cube_config"
    assert config["builder"]["dockerfile"] == "cube_config/Dockerfile"
    assert config["env"]["clear"]["CUBEJS_CACHE_AND_QUEUE_DRIVER"] == "memory"
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
    assert "SCOUT_CUBEJS_API_SECRET:" in workflow
    assert "CUBE_TAG: cube-${{ github.sha }}" in workflow
    assert workflow.index("- name: Deploy Cube") < workflow.index("- name: Deploy MCP")
