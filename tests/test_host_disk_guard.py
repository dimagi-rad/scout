"""The pre-deploy disk guard frees space safely and stops a deploy that cannot fit."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.kamal_config import load_config

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "host-disk-guard.sh"
WORKFLOWS = [("deploy.yml", None), ("deploy-staging.yml", "staging")]
KAMAL_CONFIGS = [
    "deploy.yml",
    "deploy-mcp.yml",
    "deploy-cube.yml",
    "deploy-frontend.yml",
    "deploy-worker.yml",
]

DOCKER_DOUBLE = r"""
import json, os, sys
from pathlib import Path

path = Path(os.environ["DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state["commands"].append(args)
path.write_text(json.dumps(state))
if args[0] == "ps":
    filters = [args[i + 1] for i, value in enumerate(args) if value == "--filter"]
    statuses = {f.split("=", 1)[1] for f in filters if f.startswith("status=")}
    assert statuses == {"created", "exited", "dead"}, statuses
    assert "label=service=scout-worker" in filters
    label = next(f for f in filters if f.startswith("label=destination=")).split("=", 2)[2]
    destination = label or "production"
    if f"ps {destination}" in state["fail"]:
        sys.exit(1)
    print("\n".join(state["stopped"].get(destination, [])))
elif args[0] == "rm" and args[1] in state["missing"]:
    print(f"Error response from daemon: No such container: {args[1]}", file=sys.stderr)
    sys.exit(1)
elif args[0] == "rm" and f"rm {args[1]}" in state["fail"]:
    print("Cannot connect to the Docker daemon", file=sys.stderr)
    sys.exit(1)
elif args[0] == "info":
    print(state.get("root", "/"))
"""


@pytest.fixture
def guard(tmp_path):
    state_file = tmp_path / "docker-state.json"
    home = tmp_path / "home"
    home.mkdir()
    account = tmp_path / "account"
    account.write_text(f"scout:x:1000:1000::{home}:/bin/bash")
    doubles = {
        "docker": DOCKER_DOUBLE,
        "getent": (f"from pathlib import Path\nprint(Path({str(account)!r}).read_text())\n"),
        "timeout": (
            "import os, sys\n"
            "assert sys.argv[1] == '--foreground'\n"
            "os.execvp(sys.argv[3], sys.argv[3:])\n"
        ),
        # df -P output: available space is the fourth column, in KiB.
        "df": (
            "import json, os\n"
            "from pathlib import Path\n"
            "kb = json.loads(Path(os.environ['DOCKER_STATE']).read_text())['free_kb']\n"
            "if kb is None:\n"
            "    raise SystemExit(1)\n"
            "print('Filesystem 1024-blocks Used Available Capacity Mounted on')\n"
            "print(f'/dev/root 100000000 1 {kb} 99% /')\n"
        ),
    }
    for name, body in doubles.items():
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\n{body}")
        executable.chmod(0o755)
    env = {
        "PATH": os.pathsep.join((str(tmp_path), *filter(None, os.defpath.split(os.pathsep)))),
        "DOCKER_STATE": str(state_file),
    }

    def run(*args, stopped=None, free_gb=50, fail=(), missing=()):
        free_kb = None if free_gb is None else int(free_gb * 1024 * 1024)
        state = {
            "commands": [],
            "stopped": stopped or {},
            "free_kb": free_kb,
            "fail": [*fail],
            "missing": [*missing],
        }
        state_file.write_text(json.dumps(state))
        result = subprocess.run(  # noqa: S603 - repository script against owned doubles
            ["/bin/bash", str(SCRIPT), *args],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result, json.loads(state_file.read_text())["commands"]

    run.home = home
    run.account = account
    return run


def _removed(commands):
    return [args[1] for args in commands if args[0] == "rm"]


def test_prunes_stopped_workers_beyond_retention_per_destination(guard):
    stopped = {"production": ["p1", "p2", "p3", "p4", "p5"], "staging": ["s1", "s2", "s3", "s4"]}
    result, commands = guard("prune-workers", stopped=stopped)
    assert result.returncode == 0, result.stderr
    # docker ps lists newest first; the newest three per destination survive.
    assert _removed(commands) == ["p4", "p5", "s4"]
    assert ["image", "prune", "--force"] in commands
    assert not any("-a" in args or "--all" in args for args in commands if args[0] == "image")


def test_empty_receipt_directories_do_not_block_pruning(guard):
    for destination in ("production", "staging"):
        (guard.home / ".scout-worker-drains-v1" / destination).mkdir(parents=True)
    result, commands = guard("prune-workers", stopped={"staging": ["s1", "s2", "s3", "s4"]})
    assert result.returncode == 0, result.stderr
    assert _removed(commands) == ["s4"]


@pytest.mark.parametrize(
    "blocker",
    [
        ".scout-worker-drains-v1/staging/" + "a" * 64 + "-2026-09-15T10:20:30Z",
        ".scout-worker-drains-v1/production/" + "b" * 64 + "-2026-09-15T10:20:30Z",
        ".kamal/scout-worker-drains-v1",
    ],
)
def test_any_pending_or_legacy_drain_receipt_keeps_every_stopped_worker(guard, blocker):
    (guard.home / blocker).mkdir(parents=True)
    stopped = {"production": ["p1", "p2", "p3", "p4"], "staging": ["s1", "s2", "s3", "s4"]}
    result, commands = guard("prune-workers", stopped=stopped)
    assert result.returncode == 0, result.stderr
    assert "Worker prune skipped" in result.stdout
    assert commands == []


@pytest.mark.parametrize(
    "record",
    [
        "scout:x:1000:1000::{home}:/bin/bash\nscout:x:1000:1000::/elsewhere:/bin/bash",
        "scout:x:1000:1000:{home}",
        "other:x:1000:1000::{home}:/bin/bash",
    ],
)
def test_malformed_account_record_keeps_every_stopped_worker(guard, record):
    guard.account.write_text(record.format(home=guard.home))
    result, commands = guard("prune-workers", stopped={"production": ["p1", "p2", "p3", "p4"]})
    assert result.returncode == 0, result.stderr
    assert "Invalid scout account record" in result.stdout
    assert commands == []


@pytest.mark.parametrize("kind", ["root", "symlink", "file"])
def test_unusable_home_keeps_every_stopped_worker(guard, tmp_path, kind):
    home = {"root": "/", "symlink": tmp_path / "home-link", "file": tmp_path / "home-file"}[kind]
    if kind == "symlink":
        home.symlink_to(guard.home)
    elif kind == "file":
        home.write_text("")
    guard.account.write_text(f"scout:x:1000:1000::{home}:/bin/bash")
    result, commands = guard("prune-workers", stopped={"production": ["p1", "p2", "p3", "p4"]})
    assert result.returncode == 0, result.stderr
    assert "Worker prune skipped" in result.stdout
    assert commands == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permission bits")
@pytest.mark.parametrize("unreadable", ["home", "home/.kamal"])
def test_unreadable_home_or_kamal_keeps_every_stopped_worker(guard, unreadable):
    (guard.home / ".kamal").mkdir()
    directory = guard.home.parent / unreadable
    directory.chmod(0o000)
    try:
        result, commands = guard("prune-workers", stopped={"production": ["p1", "p2", "p3", "p4"]})
    finally:
        directory.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert "Unreadable home, or legacy or unreadable drain metadata" in result.stdout
    assert commands == []


def test_listing_or_removal_failures_do_not_abort_the_rest_of_the_prune(guard):
    stopped = {"production": ["p1", "p2", "p3", "gone", "p5"], "staging": ["s1", "s2", "s3", "s4"]}
    result, commands = guard("prune-workers", stopped=stopped, fail={"rm gone"})
    assert result.returncode == 0, result.stderr
    assert _removed(commands) == ["gone", "p5", "s4"]
    assert "1 stopped production worker(s) could not be removed" in result.stdout

    result, commands = guard("prune-workers", stopped=stopped, missing={"gone"})
    assert result.returncode == 0, result.stderr
    assert "could not be removed" not in result.stdout
    assert ["image", "prune", "--force"] in commands

    result, commands = guard("prune-workers", stopped=stopped, fail={"ps production"})
    assert result.returncode == 0, result.stderr
    assert "Worker prune incomplete::Could not list stopped production workers" in result.stdout
    assert _removed(commands) == ["s4"]
    assert ["image", "prune", "--force"] in commands


def test_unexpected_receipt_root_contents_fail_closed(guard):
    root = guard.home / ".scout-worker-drains-v1"
    root.mkdir()
    (root / "stray-file").write_text("")
    result, commands = guard("prune-workers", stopped={"production": ["p1", "p2", "p3", "p4"]})
    assert result.returncode == 0, result.stderr
    assert commands == []


def test_check_passes_with_room_and_warns_when_getting_full(guard):
    result, _ = guard("check", "8", free_gb=40)
    assert result.returncode == 0, result.stderr
    assert "::warning" not in result.stdout
    result, _ = guard("check", "8", free_gb=12)
    assert result.returncode == 0, result.stderr
    assert "::warning title=Host disk getting full::12 GB free" in result.stdout


def test_unreadable_free_space_fails_with_an_annotation(guard):
    result, _ = guard("check", "8", free_gb=None)
    assert result.returncode == 1
    assert "::error title=Host disk check failed::" in result.stdout


def test_check_fails_loudly_when_disk_is_too_full(guard):
    result, _ = guard("check", "8", free_gb=3.5)
    assert result.returncode == 1
    assert "::error title=Host disk nearly full::Only 3 GB free" in result.stdout
    assert "DEPLOYMENT.md: Host disk full" in result.stdout


@pytest.mark.parametrize("args", [(), ("check",), ("check", "0"), ("check", "8;rm"), ("nope",)])
def test_rejects_bad_usage(guard, args):
    result, commands = guard(*args)
    assert result.returncode == 2
    assert commands == []


def _deploy_job(name):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    return workflow["jobs"]["deploy"]


@pytest.mark.parametrize(("name", "destination"), WORKFLOWS)
def test_disk_is_freed_and_checked_before_anything_is_pulled(name, destination):
    job = _deploy_job(name)
    names = [step.get("name") for step in job["steps"]]
    free, check = names.index("Free host disk space"), names.index("Check host disk space")
    assert names.index("Setup SSH") < free < check < names.index("Deploy Cube")
    assert check < names.index("Drain old workers")
    assert type(job["env"]["HOST_MIN_FREE_GB"]) is int

    free_run = job["steps"][free]["run"]
    kamal_prunes = re.findall(r"^\s*kamal prune .*$", free_run, re.MULTILINE)
    suffix = ["-d", "staging"] if destination else []
    assert [shlex.split(line.rstrip("\\"))[:3] for line in kamal_prunes] == [
        ["kamal", "prune", "all"],
        ["kamal", "prune", "images"],
    ]
    assert f'-c "$config"{" -d staging" if destination else ""}' in free_run
    # Service-wide container pruning could delete receipt-referenced workers.
    configs = shlex.split(free_run.split("for config in", 1)[1].split(";", 1)[0])
    assert configs == [f"config/{c}" for c in KAMAL_CONFIGS if c != "deploy-worker.yml"]
    assert shlex.split(kamal_prunes[1].rstrip("\\")) == [
        "kamal",
        "prune",
        "images",
        "-c",
        "config/deploy-worker.yml",
        *suffix,
    ]
    assert "prune-workers < scripts/host-disk-guard.sh" in free_run

    check_step = job["steps"][check]
    assert not check_step.get("continue-on-error")
    assert "||" not in check_step["run"]
    assert 'check "$HOST_MIN_FREE_GB" < scripts/host-disk-guard.sh' in check_step["run"]


@pytest.mark.parametrize("config_name", KAMAL_CONFIGS)
@pytest.mark.parametrize("destination", [None, "staging"])
def test_every_role_retains_three_stopped_containers(config_name, destination):
    assert load_config(config_name, destination=destination)["retain_containers"] == 3
    assert "WORKER_RETAIN=3\n" in SCRIPT.read_text()


@pytest.mark.parametrize(("name", "destination"), WORKFLOWS)
@pytest.mark.parametrize(
    ("failing", "expected"), [("", 0), ("ssh", 0), ("kamal", 1), ("kamal ssh", 1)]
)
def test_prune_step_tolerates_some_failures_but_not_all(
    tmp_path, name, destination, failing, expected
):
    job = _deploy_job(name)
    step = next(s for s in job["steps"] if s.get("name") == "Free host disk space")
    calls = tmp_path / "calls"
    for tool in ("kamal", "ssh"):
        executable = tmp_path / tool
        code = 1 if tool in failing.split() else 0
        executable.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit {code}\n')
        executable.chmod(0o755)
    summary = tmp_path / "summary"
    result = subprocess.run(  # noqa: S603 - checked-in step with owned command doubles
        ["/bin/bash", "-e", "-c", step["run"]],
        cwd=ROOT,
        env={
            "PATH": os.pathsep.join((str(tmp_path), *filter(None, os.defpath.split(os.pathsep)))),
            "SCOUT_EC2_IP": "example.invalid",
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    commands = len(calls.read_text().splitlines())
    assert commands == 6
    if failing:
        tools = failing.split()
        failures = (commands - 1 if "kamal" in tools else 0) + ("ssh" in tools)
        assert f"{failures} of {commands} commands failed" in summary.read_text()
    if expected:
        assert "::error title=Host prune failed::" in result.stdout


def test_failed_production_deploys_open_a_tracking_issue():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
    report = workflow["jobs"]["report"]
    assert report["needs"] == ["test", "deploy"]
    assert "always()" in report["if"]
    assert report["permissions"] == {"contents": "read", "issues": "write"}
    script = report["steps"][-1]
    assert script["env"] == {
        "TEST_RESULT": "${{ needs.test.result }}",
        "DEPLOY_RESULT": "${{ needs.deploy.result }}",
    }
    assert "deploy-failure-issue.cjs" in script["with"]["script"]
