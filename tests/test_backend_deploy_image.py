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
def test_each_backend_deploy_pulls_and_validates_its_prebuilt_version(
    name, environment, role, config_name, service
):
    workflow = _workflow(name)
    step = next(step for step in workflow["steps"] if step.get("name") == f"Deploy {role}")
    tag = f"{role.upper()}_TAG"
    # Worker redeploy deliberately omits Kamal's service-wide pruning, which
    # could otherwise erase the other destination's pending-drain evidence.
    expected_args = ["kamal", "redeploy" if role == "Worker" else "deploy"]
    if config_name != "deploy.yml":
        expected_args += ["-c", f"config/{config_name}"]
    if environment == "staging":
        expected_args += ["-d", "staging"]
    expected_args += ["--skip-push", f"--version=${tag}"]

    # --skip-push still pulls and validates the exact role-labeled image;
    # builds must have finished before any old worker receives SIGTERM.
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
def test_backend_builds_finish_before_cube_changes_or_worker_drain(
    name, environment, role, config_name, service
):
    steps = _workflow(name)["steps"]
    names = [step.get("name") for step in steps]
    build_name = f"Build and push {role} image"
    assert names.count(build_name) == 1, f"Missing role-qualified prebuild: {build_name}"
    build_index = names.index(build_name)
    expected_args = ["kamal", "build", "push"]
    if config_name != "deploy.yml":
        expected_args += ["-c", f"config/{config_name}"]
    if environment == "staging":
        expected_args += ["-d", "staging"]
    expected_args.append(f"--version=${role.upper()}_TAG")
    assert shlex.split(steps[build_index]["run"]) == expected_args
    assert names.index("Setup SSH") < build_index < names.index("Deploy Cube")
    assert build_index < names.index("Drain old workers")


@pytest.mark.parametrize(("name", "environment"), WORKFLOWS)
def test_kamal_runtime_is_pinned_to_verified_label_and_health_contract(name, environment):
    step = next(step for step in _workflow(name)["steps"] if step.get("name") == "Install Kamal")
    assert shlex.split(step["run"]) == ["gem", "install", "kamal", "--version", "2.12.0"]


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
        r"^\s*(kamal (?:setup|deploy|redeploy)\b[^\n]*)",
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


def _assert_manual_worker_sequence(block):
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    assert lines[:2] == ["(", "set -e"]
    assert lines[-1] == ")"

    def position(marker):
        positions = [
            index
            for index, line in enumerate(lines)
            if marker in line and not line.startswith("kamal build push ")
        ]
        assert len(positions) == 1, f"Expected one {marker!r} in manual sequence: {lines}"
        return positions[0]

    drain = position("scripts/drain-workers.sh")
    api = position('-api-$IMAGE_TAG"')
    mcp = position("config/deploy-mcp.yml")
    worker = position("config/deploy-worker.yml")
    assert drain < api < mcp < worker
    worker_args = shlex.split(lines[worker])
    assert worker_args[:2] == ["kamal", "redeploy"], "Worker handoff must not service-prune"
    destination = worker_args[worker_args.index("-d") + 1] if "-d" in worker_args else "production"
    assert destination in {"production", "staging"}
    assert f"bash -s -- {destination} 600 < scripts/drain-workers.sh" in lines[drain], (
        f"Drain destination does not match the {destination} worker: {lines[drain]}"
    )
    for index in (api, mcp):
        args = shlex.split(lines[index])
        selected = args[args.index("-d") + 1] if "-d" in args else "production"
        assert selected == destination, f"Backend destination mismatch: {lines[index]}"
    for index in (api, mcp, worker):
        boot_args = shlex.split(lines[index])
        assert "--skip-push" in boot_args, f"Post-drain build is forbidden: {lines[index]}"
        expected_build = [
            "kamal",
            "build",
            "push",
            *(arg for arg in boot_args[2:] if arg != "--skip-push"),
        ]
        build_positions = [i for i, line in enumerate(lines) if shlex.split(line) == expected_build]
        assert len(build_positions) == 1, f"Missing exact role prebuild: {expected_build}"
        assert build_positions[0] < drain, f"Role build happens after drain: {expected_build}"


def test_manual_worker_sequences_stop_on_failed_drain_or_api_gate():
    blocks = re.findall(r"```bash\n(.*?)```", (REPO_ROOT / "DEPLOYMENT.md").read_text(), re.DOTALL)
    sequences = [
        block for block in blocks if "kamal " in block and "config/deploy-worker.yml" in block
    ]
    assert len(sequences) == 4  # Setup/deploy, separately for production/staging.
    for block in sequences:
        _assert_manual_worker_sequence(block)


MANUAL_STAGING_SEQUENCE = """(
set -e
kamal build push -d staging --version="staging-api-$IMAGE_TAG"
kamal build push -c config/deploy-mcp.yml -d staging --version="staging-mcp-$IMAGE_TAG"
kamal build push -c config/deploy-worker.yml -d staging --version="staging-worker-$IMAGE_TAG"
ssh scout@example.invalid bash -s -- staging 600 < scripts/drain-workers.sh
kamal deploy -d staging --skip-push --version="staging-api-$IMAGE_TAG"
kamal deploy -c config/deploy-mcp.yml -d staging --skip-push --version="staging-mcp-$IMAGE_TAG"
kamal redeploy -c config/deploy-worker.yml -d staging --skip-push --version="staging-worker-$IMAGE_TAG"
)"""


def test_manual_sequence_checker_rejects_wrong_destination_drain():
    wrong = MANUAL_STAGING_SEQUENCE.replace("bash -s -- staging", "bash -s -- production")
    with pytest.raises(AssertionError, match="Drain destination does not match the staging worker"):
        _assert_manual_worker_sequence(wrong)


@pytest.mark.parametrize("marker", ["scripts/drain-workers.sh", "config/deploy-mcp.yml"])
def test_manual_sequence_checker_reports_missing_marker(marker):
    wrong = "\n".join(line for line in MANUAL_STAGING_SEQUENCE.splitlines() if marker not in line)
    with pytest.raises(AssertionError, match="Expected one") as failure:
        _assert_manual_worker_sequence(wrong)
    assert marker in str(failure.value)
