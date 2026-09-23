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
    destination = next(f for f in filters if f.startswith("label=destination=")).split("=", 2)[2]
    print("\n".join(state["stopped"].get(destination or "production", [])))
elif args[0] == "info":
    print(state.get("root", "/"))
"""


@pytest.fixture
def guard(tmp_path):
    state_file = tmp_path / "docker-state.json"
    home = tmp_path / "home"
    home.mkdir()
    doubles = {
        "docker": DOCKER_DOUBLE,
        "getent": f"print('scout:x:1000:1000::{home}:/bin/bash')",
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

    def run(*args, stopped=None, free_gb=50):
        state_file.write_text(
            json.dumps(
                {"commands": [], "stopped": stopped or {}, "free_kb": int(free_gb * 1024 * 1024)}
            )
        )
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


def _steps(name):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    return workflow["jobs"]["deploy"]


@pytest.mark.parametrize(("name", "destination"), WORKFLOWS)
def test_disk_is_freed_and_checked_before_anything_is_pulled(name, destination):
    job = _steps(name)
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
    assert shlex.split(kamal_prunes[1].rstrip("\\")) == [
        "kamal",
        "prune",
        "images",
        "-c",
        "config/deploy-worker.yml",
        *suffix,
    ]
    # Service-wide container pruning could delete receipt-referenced workers.
    assert "deploy-worker.yml" not in free_run.split("for config in", 1)[1].split("\n")[0]
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
