"""Backend image versions must not be overwritten by another Kamal service."""

import re
import shlex
from pathlib import Path

import pytest
import yaml

from tests.kamal_config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = [("deploy.yml", "production"), ("deploy-staging.yml", "staging")]
BACKENDS = [
    ("API", "deploy.yml", "scout"),
    ("MCP", "deploy-mcp.yml", "scout-mcp"),
    ("Worker", "deploy-worker.yml", "scout-worker"),
]


def _workflow(name):
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())["jobs"][
        "deploy"
    ]


@pytest.mark.parametrize(("name", "environment"), WORKFLOWS)
@pytest.mark.parametrize(("role", "config_name", "service"), BACKENDS)
def test_each_backend_deploy_builds_and_validates_its_own_version(
    name, environment, role, config_name, service
):
    workflow = _workflow(name)
    step = next(step for step in workflow["steps"] if step.get("name") == f"Deploy {role}")
    tag = f"{role.upper()}_TAG"
    expected_args = ["kamal", "deploy"]
    if config_name != "deploy.yml":
        expected_args += ["-c", f"config/{config_name}"]
    if environment == "staging":
        expected_args += ["-d", "staging"]
    expected_args.append(f"--version=${tag}")

    # Normal Kamal deploy builds the role's labeled image, then pulls and
    # validates that exact version. Do not skip its build or its label check.
    assert shlex.split(step["run"]) == expected_args
    assert workflow["env"][tag] == f"{environment}-{role.lower()}-${{{{ github.sha }}}}"
    config = load_config(config_name, destination="staging" if environment == "staging" else None)
    assert config["image"] == "scout/api"
    assert config["service"] == service
    assert config["builder"]["arch"] == "amd64"


def test_backend_versions_cannot_collide_across_roles_or_destinations():
    sha = "0123456789abcdef" * 2 + "01234567"
    versions = [
        _workflow(name)["env"][f"{role.upper()}_TAG"].replace("${{ github.sha }}", sha)
        for name, _ in WORKFLOWS
        for role, _, _ in BACKENDS
    ]
    assert len(set(versions)) == 6
    assert sha not in versions
    assert all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) for tag in versions)


@pytest.mark.parametrize(("name", "environment"), WORKFLOWS)
def test_no_unlabeled_backend_prebuild_and_dependency_order_is_preserved(name, environment):
    steps = _workflow(name)["steps"]
    assert not any(step.get("name") == "Build and push API image" for step in steps)
    assert not any("scout/api:" in step.get("run", "") for step in steps)
    assert [step["name"] for step in steps if step.get("name", "").startswith("Deploy ")] == [
        "Deploy Cube",
        "Deploy API",
        "Deploy MCP",
        "Deploy Worker",
        "Deploy Frontend",
    ]


@pytest.mark.parametrize(("name", "environment"), WORKFLOWS)
@pytest.mark.parametrize(("role", "config_name", "service"), BACKENDS)
def test_runtime_release_remains_the_commit_sha_not_the_image_version(
    name, environment, role, config_name, service
):
    assert _workflow(name)["env"]["IMAGE_TAG"] == "${{ github.sha }}"
    config = load_config(config_name, destination="staging" if environment == "staging" else None)
    assert config["env"]["clear"]["SENTRY_RELEASE"] == "<%= ENV.fetch('IMAGE_TAG', '') %>"
    assert config["env"]["clear"]["SENTRY_ENVIRONMENT"] == environment


def test_manual_backend_commands_also_use_role_and_destination_qualified_versions():
    commands = re.findall(
        r"^\s*(kamal (?:setup|deploy)\b[^\n]*)",
        (REPO_ROOT / "DEPLOYMENT.md").read_text(),
        re.MULTILINE,
    )
    checked = set()
    for command in commands:
        args = shlex.split(command)
        config_name = Path(args[args.index("-c") + 1]).name if "-c" in args else "deploy.yml"
        backend = next((item for item in BACKENDS if item[1] == config_name), None)
        if backend is None:
            continue
        role = backend[0].lower()
        environment = args[args.index("-d") + 1] if "-d" in args else "production"
        assert f"--version={environment}-{role}-$IMAGE_TAG" in args
        checked.add((environment, role))
    assert checked == {
        (environment, role.lower()) for _, environment in WORKFLOWS for role, _, _ in BACKENDS
    }


def test_manual_worker_sequences_stop_on_failed_drain_or_api_gate():
    blocks = re.findall(r"```bash\n(.*?)```", (REPO_ROOT / "DEPLOYMENT.md").read_text(), re.DOTALL)
    sequences = [
        block for block in blocks if "kamal " in block and "config/deploy-worker.yml" in block
    ]
    assert len(sequences) == 4  # Setup/deploy, separately for production/staging.
    for block in sequences:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        assert lines[:2] == ["(", "set -e"]
        assert lines[-1] == ")"
        drain = next(
            index for index, line in enumerate(lines) if "scripts/drain-workers.sh" in line
        )
        api = next(index for index, line in enumerate(lines) if '-api-$IMAGE_TAG"' in line)
        mcp = next(index for index, line in enumerate(lines) if "config/deploy-mcp.yml" in line)
        worker = next(
            index for index, line in enumerate(lines) if "config/deploy-worker.yml" in line
        )
        assert drain < api < mcp < worker
