"""Fitness tests for the production Cube/semantic deployment seam."""

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_config(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "config" / name).read_text())


def test_production_services_share_the_cube_runtime_and_signing_secret():
    for name in ("deploy.yml", "deploy-worker.yml", "deploy-mcp.yml"):
        config = _load_config(name)
        assert config["env"]["clear"]["CUBE_API_URL"] == "http://scout-cube-web:4000"
        assert "CUBEJS_API_SECRET" in config["env"]["secret"]

    for name in ("deploy.yml", "deploy-worker.yml"):
        clear = _load_config(name)["env"]["clear"]
        assert clear["CUBE_VALIDATOR_URL"] == "http://scout-cube-web:4010"
        assert clear["CUBE_SCHEMA_VALIDATION_REQUIRED"] == "True"


def test_production_cube_is_internal_and_uses_existing_infrastructure():
    config = _load_config("deploy-cube.yml")
    assert config["image"] == "scout/mcp"
    assert config["builder"]["context"] == "cube_config"
    assert config["builder"]["dockerfile"] == "cube_config/Dockerfile"
    assert config["env"]["clear"]["CUBEJS_CACHE_AND_QUEUE_DRIVER"] == "memory"
    server = config["servers"]["web"]
    assert server["proxy"] is False
    assert server["options"]["network"] == "scout_shared"
    assert server["options"]["network-alias"] == "scout-cube-web"
    assert set(config["env"]["secret"]) == {
        "DATABASE_URL",
        "MANAGED_DATABASE_URL",
        "CUBEJS_API_SECRET",
    }


def test_production_workflow_requires_and_deploys_cube_before_dependents():
    workflow = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "aws secretsmanager get-secret-value" in workflow
    assert "--secret-id SCOUT_CUBEJS_API_SECRET" in workflow
    assert 'test -n "$cube_secret"' in workflow
    assert "CUBE_TAG: cube-${{ github.sha }}" in workflow
    assert workflow.index("- name: Deploy Cube") < workflow.index("- name: Deploy MCP")


def test_kamal_uses_the_environment_aware_cube_secret_resolver():
    secrets_file = (REPO_ROOT / ".kamal" / "secrets-common").read_text()
    assert "CUBEJS_API_SECRET=$(scripts/resolve-cube-secret.sh)" in secrets_file


def test_cube_secret_resolver_fetches_production_secret_from_aws(tmp_path):
    fake_kamal = tmp_path / "kamal"
    fake_kamal.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "secrets fetch" ]; then\n'
        "  printf 'encrypted-bundle'\n"
        'elif [ "$1 $2" = "secrets extract" ]; then\n'
        "  printf 'production-secret'\n"
        "else\n"
        "  exit 2\n"
        "fi\n"
    )
    fake_kamal.chmod(0o755)
    env = {"PATH": str(tmp_path)}

    result = subprocess.run(  # noqa: S603 - fixed repository script and test PATH
        [REPO_ROOT / "scripts" / "resolve-cube-secret.sh"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.stdout == "production-secret"


def test_cube_secret_resolver_prefers_staging_override_without_kamal():
    env = {"PATH": "", "SCOUT_CUBEJS_API_SECRET": "staging-secret"}

    result = subprocess.run(  # noqa: S603 - fixed repository script, no shell
        [REPO_ROOT / "scripts" / "resolve-cube-secret.sh"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.stdout == "staging-secret"
