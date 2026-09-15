"""The frontend deploy must use the image built with the workflow's telemetry inputs."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.kamal_config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = [("deploy.yml", None, "IMAGE_TAG"), ("deploy-staging.yml", "staging", "FRONTEND_TAG")]


def _workflow(name):
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())


def _step(workflow, name):
    return next(step for step in workflow["jobs"]["deploy"]["steps"] if step.get("name") == name)


def _build_args(step):
    # Only inspect the Docker build command, not shell guards or the later push.
    command = step["run"].split("docker build ", 1)[1].split("docker push ", 1)[0]
    return shlex.split(command.replace("\\\n", ""))


@pytest.mark.parametrize(("name", "destination", "tag"), WORKFLOWS)
def test_frontend_deploy_pulls_the_exact_prebuilt_environment_image(name, destination, tag):
    workflow = _workflow(name)
    build = _step(workflow, "Build and push Frontend image")
    deploy = _step(workflow, "Deploy Frontend")
    build_args = _build_args(build)
    deploy_args = shlex.split(deploy["run"])
    config = load_config("deploy-frontend.yml", destination=destination)

    assert build["run"].count("docker build ") == 1
    assert build_args[build_args.index("-t") + 1] == f"$SCOUT_ECR_REGISTRY/scout/frontend:${tag}"
    assert f'docker push "$SCOUT_ECR_REGISTRY/scout/frontend:${tag}"' in build["run"]
    assert deploy_args[:2] == ["kamal", "deploy"]
    assert "--skip-push" in deploy_args
    assert f"--version=${tag}" in deploy_args
    assert deploy_args[deploy_args.index("-c") + 1] == "config/deploy-frontend.yml"
    if destination:
        assert deploy_args[deploy_args.index("-d") + 1] == destination
        assert workflow["jobs"]["deploy"]["env"][tag] == "staging-${{ github.sha }}"
    else:
        assert "-d" not in deploy_args
        assert workflow["jobs"]["deploy"]["env"][tag] == "${{ github.sha }}"

    # Kamal build:pull validates this exact service label even with --skip-push.
    assert build_args[build_args.index("--label") + 1] == f"service={config['service']}"
    assert build_args[build_args.index("--platform") + 1] == f"linux/{config['builder']['arch']}"
    assert f"NGINX_CONF={config['builder']['args']['NGINX_CONF']}" in build_args
    steps = workflow["jobs"]["deploy"]["steps"]
    assert steps.index(build) < steps.index(deploy)


@pytest.mark.parametrize(("name", "destination", "tag"), WORKFLOWS)
def test_frontend_build_bakes_in_its_explicit_monitoring_inputs(name, destination, tag):
    workflow = _workflow(name)
    build = _step(workflow, "Build and push Frontend image")
    args = _build_args(build)

    assert build["env"]["VITE_SENTRY_DSN"] == "${{ secrets.SCOUT_VITE_SENTRY_DSN }}"
    # Docker reads the DSN from the step environment; never put its value in the command.
    assert args[args.index("VITE_SENTRY_DSN") - 1] == "--build-arg"
    assert not any(arg.startswith("VITE_SENTRY_DSN=") for arg in args)
    assert f"VITE_SENTRY_ENVIRONMENT={destination or 'production'}" in args
    assert "VITE_SENTRY_RELEASE=$IMAGE_TAG" in args
    assert workflow["jobs"]["deploy"]["env"]["IMAGE_TAG"] == "${{ github.sha }}"


@pytest.mark.parametrize(("name", "destination", "tag"), WORKFLOWS)
@pytest.mark.parametrize("missing", ["VITE_SENTRY_DSN", "IMAGE_TAG"])
def test_missing_monitoring_input_stops_before_any_build(name, destination, tag, missing):
    build = _step(_workflow(name), "Build and push Frontend image")
    guard = build["run"].split("docker build ", 1)[0]
    env = {"PATH": os.defpath, "VITE_SENTRY_DSN": "synthetic-public-dsn", "IMAGE_TAG": "test-sha"}
    env[missing] = ""
    result = subprocess.run(  # noqa: S603 - fixed bash, workflow guard, synthetic environment
        ["/bin/bash", "-eu", "-c", guard], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert missing in result.stderr
    assert "synthetic-public-dsn" not in result.stdout + result.stderr


@pytest.mark.parametrize(("name", "destination", "tag"), WORKFLOWS)
def test_present_monitoring_inputs_pass_without_logging_the_dsn(name, destination, tag):
    guard = _step(_workflow(name), "Build and push Frontend image")["run"].split(
        "docker build ", 1
    )[0]
    result = subprocess.run(  # noqa: S603 - fixed bash and workflow guard; no deploy commands
        ["/bin/bash", "-eu", "-c", guard],
        capture_output=True,
        text=True,
        env={
            "PATH": os.defpath,
            "VITE_SENTRY_DSN": "synthetic-public-dsn",
            "IMAGE_TAG": "test-sha",
        },
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_production_source_map_upload_credentials_stay_buildkit_only():
    build = _step(_workflow("deploy.yml"), "Build and push Frontend image")
    args = _build_args(build)
    assert build["env"]["SENTRY_AUTH_TOKEN"] == "${{ secrets.SCOUT_SENTRY_AUTH_TOKEN }}"
    assert "id=sentry_auth_token,env=SENTRY_AUTH_TOKEN" in args
    assert "SENTRY_ORG=${{ vars.SCOUT_SENTRY_ORG }}" in args
    assert "SENTRY_PROJECT=${{ vars.SCOUT_SENTRY_FRONTEND_PROJECT }}" in args
    assert not any(arg.startswith("SENTRY_AUTH_TOKEN=") for arg in args)
