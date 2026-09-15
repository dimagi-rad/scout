"""Execute the workflow handoff with isolated commands and build failures."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLES = ("API", "MCP", "Worker")


def _workflow(destination):
    filename = "deploy-staging.yml" if destination == "staging" else "deploy.yml"
    return yaml.safe_load((REPO_ROOT / ".github/workflows" / filename).read_text())


def _assert_default_success_gating(steps):
    """The executable model below only represents default bash/success gating."""
    for step in steps:
        assert not step.get("continue-on-error", False), step["name"]
        assert step.get("if", "success()") == "success()", step["name"]
        assert step.get("shell", "bash") == "bash", step["name"]
        assert not step.get("env"), step["name"]


def test_both_destinations_share_a_non_cancelling_host_queue():
    production = _workflow("production")["concurrency"]
    staging = _workflow("staging")["concurrency"]
    assert production == staging, "Both workflows deploy to the same host"
    assert isinstance(production["group"], str) and production["group"]
    assert "${{" not in production["group"], "The host lock must not vary by workflow/ref"
    assert production["cancel-in-progress"] is False
    # The default single pending slot can silently cancel the other destination.
    assert production["queue"] == "max"


@pytest.mark.parametrize("destination", ["production", "staging"])
@pytest.mark.parametrize("role", [*ROLES, "Frontend"])
def test_post_drain_deployments_have_an_explicit_bounded_step_timeout(destination, role):
    steps = _workflow(destination)["jobs"]["deploy"]["steps"]
    step = next(step for step in steps if step.get("name") == f"Deploy {role}")
    timeout = step.get("timeout-minutes")
    assert type(timeout) is int and 1 <= timeout <= 15, step["name"]
    assert not step.get("continue-on-error", False), step["name"]


@pytest.mark.parametrize(
    "override",
    [
        {"continue-on-error": True},
        {"if": "always()"},
        {"shell": "sh"},
        {"env": {"API_TAG": "unexpected-version"}},
    ],
)
def test_preflight_model_rejects_unmodelled_workflow_step_semantics(override):
    step = {"name": "Build and push API image", "run": "kamal build push", **override}
    with pytest.raises(AssertionError, match="Build and push API image"):
        _assert_default_success_gating([step])


@pytest.mark.parametrize("destination", ["production", "staging"])
@pytest.mark.parametrize("failed_role", [None, *ROLES])
def test_backend_build_failure_never_drains_workers(destination, failed_role, tmp_path):
    workflow = _workflow(destination)
    steps = workflow["jobs"]["deploy"]["steps"]
    selected_names = {
        *(f"Build and push {role} image" for role in ROLES),
        *(f"Deploy {role}" for role in ROLES),
        "Drain old workers",
    }
    selected = [step for step in steps if step.get("name") in selected_names]
    assert len(selected) == 7, "All three role prebuilds must be present in the real workflow"
    assert not workflow.get("defaults"), "The model assumes GitHub's workflow defaults"
    assert not workflow["jobs"]["deploy"].get("defaults"), "The model assumes job defaults"
    _assert_default_success_gating(selected)
    events = tmp_path / "events"
    fake = f"""#!{sys.executable}
import os
import pathlib
import sys

args = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == 'ssh':
    event = 'drain'
else:
    assert pathlib.Path(sys.argv[0]).name == 'kamal'
    assert args[:2] == ['build', 'push'] or args[0] in {{'deploy', 'redeploy'}}, args
    role = 'API'
    if '-c' in args:
        role = {{'config/deploy-mcp.yml': 'MCP', 'config/deploy-worker.yml': 'Worker'}}[args[args.index('-c') + 1]]
    event = ('build' if args[0] == 'build' else 'deploy') + ':' + role
    if args[0] in {{'deploy', 'redeploy'}}:
        assert '--skip-push' in args, args
        assert args[0] == ('redeploy' if role == 'Worker' else 'deploy'), args
with open(os.environ['SCOUT_PREFLIGHT_EVENTS'], 'a') as handle:
    handle.write(event + '\\n')
if event == 'build:' + os.environ.get('SCOUT_PREFLIGHT_FAIL_ROLE', ''):
    sys.exit(42)
"""
    for name in ("kamal", "ssh"):
        executable = tmp_path / name
        executable.write_text(fake)
        executable.chmod(0o700)
    result = subprocess.run(  # noqa: S603 - checked-in workflow with only owned fake commands in PATH
        ["/bin/bash", "-euo", "pipefail", "-c", "\n".join(step["run"] for step in selected)],
        cwd=REPO_ROOT,
        env={
            "PATH": os.fspath(tmp_path),
            "SCOUT_PREFLIGHT_EVENTS": os.fspath(events),
            "SCOUT_PREFLIGHT_FAIL_ROLE": failed_role or "",
            "SCOUT_EC2_IP": "example.invalid",
            **{f"{role.upper()}_TAG": f"{destination}-{role.lower()}-test" for role in ROLES},
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    observed = events.read_text().splitlines() if events.exists() else []
    if failed_role is not None:
        assert result.returncode == 42, result.stderr
        assert observed == [f"build:{role}" for role in ROLES[: ROLES.index(failed_role) + 1]]
    else:
        assert result.returncode == 0, result.stderr
        assert observed == [
            *(f"build:{role}" for role in ROLES),
            "drain",
            *(f"deploy:{role}" for role in ROLES),
        ]
