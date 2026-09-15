"""Exercise the real drain shell with an isolated, stateful Docker CLI double."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKER = "a" * 64
OTHER = "b" * 64
STARTED = "2026-09-15T10:20:30.123456789Z"
RECEIPT_ROOT = Path(".kamal/scout-worker-drains-v1")

DOCKER_DOUBLE = r"""
import json, os, sys
from pathlib import Path

path = Path(os.environ["DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state.setdefault("commands", []).append(args)
containers = state["containers"]

def done(code=0, output=""):
    path.write_text(json.dumps(state))
    print(output, end="")
    raise SystemExit(code)

if args[0] == "ps":
    if state.get("fail_inventory"):
        done(1)
    if state.get("new_worker_after_signal") and any(c.get("signals") for c in containers.values()):
        containers["c" * 64] = {"status": "running", "destination": "staging"}
    filters = [args[i + 1] for i, value in enumerate(args) if value == "--filter"]
    assert "label=service=scout-worker" in filters and "label=role=web" in filters
    destination = next(value.split("=", 2)[2] for value in filters if value.startswith("label=destination="))
    selected = []
    for ident, container in containers.items():
        if container.get("status", "running") not in {"running", "restarting", "paused"}:
            continue
        if not state.get("ignore_filters") and (
            container.get("absent_destination") or
            container.get("destination", "") != destination or
            container.get("service", "scout-worker") != "scout-worker" or
            container.get("role", "web") != "web"
        ):
            continue
        selected.append(ident)
    done(output="\n".join(selected) + ("\n" if selected else ""))

ident = args[-1] if args[0] in {"inspect", "kill"} else args[1]
if ident not in containers:
    done(1)
container = containers[ident]
if args[0] == "inspect":
    assert args[1] == "--format" and ".Config.Env" not in args[2]
    if container.get("signals") and not container.get("held"):
        container["status"] = "exited"
    if container.get("signals") and container.get("restart_after_signal"):
        container["started"] = "2026-09-15T10:21:30.123456789Z"
    if list(Path(".kamal/scout-worker-drains-v1").glob("*/*")) and container.get("restart_after_marker"):
        container["started"] = "2026-09-15T10:21:30.123456789Z"
    done(output="|".join([
        ident, container.get("service", "scout-worker"), container.get("role", "web"),
        container.get("destination", ""), container.get("status", "running"),
        container.get("started", "2026-09-15T10:20:30.123456789Z"),
        str(container.get("exit", 0)), str(container.get("oom", False)).lower(),
        "" if container.get("absent_destination") else "present",
    ]) + "\n")
if args[0] == "kill":
    assert args == ["kill", "--signal=TERM", ident]
    destination = "staging" if container.get("destination") else "production"
    started = container.get("started", "2026-09-15T10:20:30.123456789Z")
    receipt = Path(".kamal/scout-worker-drains-v1") / destination / f"{ident}-{started}"
    assert receipt.is_dir() and not receipt.is_symlink()
    assert receipt.stat().st_mode & 0o777 == 0o700
    assert not list(receipt.iterdir())
    container["signal_calls"] = container.get("signal_calls", 0) + 1
    if container.get("signal_failure"):
        done(1)
    container["signals"] = container.get("signals", 0) + 1
    done(output=ident + "\n")
raise AssertionError(args)
"""


@pytest.fixture
def drain_cli(tmp_path):
    state_file = tmp_path / "docker-state.json"
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + DOCKER_DOUBLE)
    docker.chmod(0o755)
    timeout = tmp_path / "timeout"
    timeout.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:3] == ['--foreground', '15s']\n"
        "os.execvp(sys.argv[3], sys.argv[3:])\n"
    )
    timeout.chmod(0o755)
    env = {
        "PATH": os.pathsep.join((str(tmp_path), os.defpath)),
        "DOCKER_STATE": str(state_file),
    }

    def run(containers=None, *, destination="staging", budget="1", options=None, reset=True):
        if reset:
            state_file.write_text(json.dumps({"containers": containers or {}, **(options or {})}))
        result = subprocess.run(  # noqa: S603 - real repository shell; only fake Docker can execute
            ["/bin/bash", str(ROOT / "scripts" / "drain-workers.sh"), destination, budget],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result, json.loads(state_file.read_text())

    return run


def pending_receipt(tmp_path, *, destination="staging", ident=WORKER, started=STARTED):
    """Owned test metadata with the exact private on-host directory contract."""
    receipt = tmp_path / RECEIPT_ROOT / destination / f"{ident}-{started}"
    receipt.mkdir(mode=0o700, parents=True)
    for directory in (receipt.parent, receipt.parent.parent):
        directory.chmod(0o700)
    return receipt


def amend_container(tmp_path, ident=WORKER, **changes):
    path = tmp_path / "docker-state.json"
    state = json.loads(path.read_text())
    state["containers"][ident].update(changes)
    path.write_text(json.dumps(state))


def test_empty_destination_is_a_successful_first_deploy(drain_cli):
    result, state = drain_cli()
    assert result.returncode == 0, result.stderr
    assert "exited cleanly" in result.stdout
    assert all(command[0] == "ps" for command in state["commands"])


@pytest.mark.parametrize("destination,label", [("production", ""), ("staging", "staging")])
def test_all_old_versions_are_signalled_once_without_touching_other_destination(
    drain_cli, tmp_path, destination, label
):
    result, state = drain_cli(
        {
            WORKER: {"destination": label},
            OTHER: {"destination": label},
            "c" * 64: {"destination": "staging" if not label else ""},
        },
        destination=destination,
    )
    assert result.returncode == 0, result.stderr
    for ident in (WORKER, OTHER):
        assert state["containers"][ident]["signals"] == 1
    assert not list((tmp_path / RECEIPT_ROOT / destination).iterdir())
    assert "signals" not in state["containers"]["c" * 64]
    assert {command[0] for command in state["commands"]} <= {"ps", "inspect", "kill"}


def test_timeout_and_retry_do_not_signal_twice_or_abort_running_jobs(drain_cli):
    result, state = drain_cli({WORKER: {"destination": "staging", "held": True}})
    assert result.returncode != 0
    assert "timed out" in result.stderr
    assert "queued jobs wait" in result.stderr
    assert state["containers"][WORKER]["signals"] == 1
    retried, state = drain_cli(reset=False)
    assert retried.returncode != 0
    assert "without another signal" in retried.stdout
    assert state["containers"][WORKER]["signal_calls"] == 1
    assert state["containers"][WORKER].get("status", "running") == "running"


@pytest.mark.parametrize("wrong", [{"destination": ""}, {"service": "other"}, {"role": "other"}])
def test_labels_are_revalidated_before_any_mutation(drain_cli, wrong):
    result, state = drain_cli(
        {WORKER: {"destination": "staging"}, OTHER: {"destination": "staging", **wrong}},
        options={"ignore_filters": True},
    )
    assert result.returncode != 0
    assert "labels do not match" in result.stderr
    assert not any(command[0] in {"exec", "kill"} for command in state["commands"])


@pytest.mark.parametrize("status", ["paused", "restarting"])
def test_ambiguous_initial_worker_state_fails_without_mutation(drain_cli, status):
    result, state = drain_cli({WORKER: {"destination": "staging", "status": status}})
    assert result.returncode != 0
    assert not any(command[0] in {"exec", "kill"} for command in state["commands"])


def test_container_start_time_cannot_inject_shell_commands(drain_cli):
    result, state = drain_cli({WORKER: {"destination": "staging", "started": "$(echo injected)"}})
    assert result.returncode != 0
    assert "Invalid worker process start time" in result.stderr
    assert not any(command[0] in {"exec", "kill"} for command in state["commands"])


@pytest.mark.parametrize("options", [{"exit": 1}, {"oom": True}, {"restart_after_signal": True}])
def test_only_the_same_cleanly_exited_process_opens_the_gate(drain_cli, options):
    result, _ = drain_cli({WORKER: {"destination": "staging", **options}})
    assert result.returncode != 0
    assert "No new publishers may start" in result.stderr


@pytest.mark.parametrize("failure", [{"exit": 1}, {"oom": True}])
def test_failed_worker_is_not_forgotten_on_a_fresh_retry(drain_cli, failure):
    first, _ = drain_cli({WORKER: {"destination": "staging", **failure}})
    assert first.returncode != 0
    retried, state = drain_cli(reset=False)
    assert retried.returncode != 0
    assert state["containers"][WORKER]["signal_calls"] == 1


@pytest.mark.parametrize("failure", [{"exit": 1}, {"oom": True}])
def test_timeout_then_failed_exit_remains_a_blocker_on_retry(drain_cli, tmp_path, failure):
    first, _ = drain_cli({WORKER: {"destination": "staging", "held": True}})
    assert first.returncode != 0 and "timed out" in first.stderr
    amend_container(tmp_path, held=False, **failure)
    retried, state = drain_cli(reset=False)
    assert retried.returncode != 0 and "exited unsuccessfully" in retried.stderr
    assert state["containers"][WORKER]["signal_calls"] == 1
    assert (tmp_path / RECEIPT_ROOT / "staging" / f"{WORKER}-{STARTED}").is_dir()


def test_clean_retry_clears_only_its_exact_receipt(drain_cli, tmp_path):
    other_receipt = pending_receipt(tmp_path, destination="production", ident=OTHER)
    first, _ = drain_cli({WORKER: {"destination": "staging", "held": True}})
    assert first.returncode != 0
    amend_container(tmp_path, held=False)
    retried, state = drain_cli(reset=False)
    assert retried.returncode == 0, retried.stderr
    assert state["containers"][WORKER]["signal_calls"] == 1
    assert not list((tmp_path / RECEIPT_ROOT / "staging").iterdir())
    assert other_receipt.is_dir()
    assert not any(OTHER in command for command in state["commands"])


@pytest.mark.parametrize("condition", ["missing", "restarted", "failed", "absent-label"])
def test_pending_receipt_is_revalidated_before_signalling_another_worker(
    drain_cli, tmp_path, condition
):
    receipt = pending_receipt(tmp_path)
    containers = {OTHER: {"destination": "staging"}}
    if condition != "missing":
        containers[WORKER] = {"destination": "staging", "status": "exited"}
        if condition == "restarted":
            containers[WORKER]["started"] = "2026-09-15T11:20:30.123456789Z"
        elif condition == "failed":
            containers[WORKER]["exit"] = 1
        else:
            containers[WORKER]["absent_destination"] = True
    result, state = drain_cli(containers)
    assert result.returncode != 0
    assert receipt.is_dir()
    assert not any(command[0] == "kill" for command in state["commands"])


def test_duplicate_process_receipt_is_not_ignored(drain_cli, tmp_path):
    current = pending_receipt(tmp_path)
    obsolete = pending_receipt(tmp_path, started="2026-09-15T09:20:30.123456789Z")
    result, state = drain_cli({WORKER: {"destination": "staging"}})
    assert result.returncode != 0 and "restarted" in result.stderr
    assert current.is_dir() and obsolete.is_dir()
    assert not any(command[0] == "kill" for command in state["commands"])


@pytest.mark.parametrize(
    "kind", ["bad-name", "hidden-name", "file", "nonempty", "symlink", "public"]
)
def test_malformed_receipt_fails_closed_without_signal_or_cleanup(drain_cli, tmp_path, kind):
    receipt = pending_receipt(tmp_path)
    if kind in {"bad-name", "hidden-name"}:
        renamed = receipt.with_name(".hidden" if kind == "hidden-name" else "not-a-container")
        receipt.rename(renamed)
        receipt = renamed
    elif kind in {"file", "symlink"}:
        receipt.rmdir()
        if kind == "file":
            receipt.write_text("unexpected")
        else:
            target = tmp_path / "unrelated"
            target.mkdir()
            (target / "keep").write_text("unrelated content")
            receipt.symlink_to(target, target_is_directory=True)
    elif kind == "nonempty":
        (receipt / "unexpected").write_text("keep")
    else:
        receipt.chmod(0o755)
    result, state = drain_cli({WORKER: {"destination": "staging"}})
    assert result.returncode != 0
    assert receipt.exists()
    if kind == "symlink":
        assert (tmp_path / "unrelated" / "keep").read_text() == "unrelated content"
    assert not any(command[0] == "kill" for command in state.get("commands", []))


@pytest.mark.parametrize("level", [".kamal", str(RECEIPT_ROOT), str(RECEIPT_ROOT / "staging")])
def test_symlinked_metadata_path_never_traverses_outside_receipt_scope(drain_cli, tmp_path, level):
    target = tmp_path / "unrelated"
    target.mkdir()
    (target / "keep").write_text("unrelated content")
    link = tmp_path / level
    link.parent.mkdir(parents=True, exist_ok=True)
    for directory in (tmp_path / ".kamal", tmp_path / RECEIPT_ROOT):
        if directory.is_dir():
            directory.chmod(0o700)
    link.symlink_to(target, target_is_directory=True)
    result, state = drain_cli({WORKER: {"destination": "staging"}})
    assert result.returncode != 0
    assert (target / "keep").read_text() == "unrelated content"
    assert len(list(target.iterdir())) == 1
    assert not state.get("commands")


def test_pending_receipt_with_unknown_signal_outcome_is_observation_only(drain_cli, tmp_path):
    receipt = pending_receipt(tmp_path)
    result, state = drain_cli({WORKER: {"destination": "staging", "held": True}})
    assert result.returncode != 0 and "timed out" in result.stderr
    assert receipt.is_dir()
    assert not any(command[0] == "kill" for command in state["commands"])


def test_new_worker_appearing_during_drain_prevents_deployment(drain_cli):
    result, state = drain_cli(
        {WORKER: {"destination": "staging"}}, options={"new_worker_after_signal": True}
    )
    assert result.returncode != 0
    assert "different old worker appeared" in result.stderr
    assert "signals" not in state["containers"]["c" * 64]


def test_uncertain_signal_failure_is_never_retried_as_a_second_signal(drain_cli):
    result, state = drain_cli({WORKER: {"destination": "staging", "signal_failure": True}})
    assert result.returncode != 0
    assert state["containers"][WORKER]["signal_calls"] == 1
    retried, state = drain_cli(reset=False)
    assert retried.returncode != 0
    assert state["containers"][WORKER]["signal_calls"] == 1


def test_failed_inventory_cannot_report_a_successful_drain(drain_cli):
    result, state = drain_cli(options={"fail_inventory": True})
    assert result.returncode != 0
    assert len(state["commands"]) == 1


def test_receipt_metadata_write_failure_prevents_signal(drain_cli, tmp_path):
    (tmp_path / ".kamal").write_text("not a directory")
    result, state = drain_cli({WORKER: {"destination": "staging"}})
    assert result.returncode != 0
    assert not any(command[0] == "kill" for command in state.get("commands", []))


def test_changed_process_after_marker_is_not_signalled(drain_cli):
    result, state = drain_cli({WORKER: {"destination": "staging", "restart_after_marker": True}})
    assert result.returncode != 0
    assert not any(command[0] == "kill" for command in state["commands"])


@pytest.mark.parametrize("budget", ["0", "601", "99999999999999999999", "01", "$(echo bad)"])
def test_invalid_budget_is_rejected_before_any_docker_operation(drain_cli, budget):
    result, state = drain_cli(budget=budget)
    assert result.returncode != 0
    assert not state.get("commands")


@pytest.mark.parametrize("destination", ["unknown", "staging; echo bad", ""])
def test_unknown_destination_is_rejected_before_any_docker_operation(drain_cli, destination):
    result, state = drain_cli(destination=destination)
    assert result.returncode != 0
    assert not state.get("commands")


def test_installed_procrastinate_sigterm_finishes_running_job_without_claiming_next(tmp_path):
    """Use the real worker/signal handler, but only its in-memory test connector."""
    script = tmp_path / "worker.py"
    script.write_text(
        "import asyncio, json, sys\n"
        "from pathlib import Path\n"
        "from procrastinate import App\n"
        "from procrastinate.testing import InMemoryConnector\n"
        "root = Path(sys.argv[1])\n"
        "connector = InMemoryConnector()\n"
        "app = App(connector=connector)\n"
        "@app.task\n"
        "async def held():\n"
        "    (root / 'started').touch()\n"
        "    while not (root / 'release').exists():\n"
        "        await asyncio.sleep(0.01)\n"
        "    (root / 'completed').touch()\n"
        "@app.task\n"
        "async def queued():\n"
        "    (root / 'unexpected-claim').touch()\n"
        "async def main():\n"
        "    async with app.open_async():\n"
        "        first = await held.defer_async()\n"
        "        second = await queued.defer_async()\n"
        "        await app.run_worker_async(concurrency=1, listen_notify=False, fetch_job_polling_interval=0.01)\n"
        "        print(json.dumps([connector.jobs[first]['status'], connector.jobs[second]['status']]))\n"
        "asyncio.run(main())\n"
    )
    process = subprocess.Popen(  # noqa: S603 - owned synthetic in-memory worker, no credentials
        [sys.executable, str(script), str(tmp_path)],
        env={"PATH": os.defpath},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "started").exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.01)
        assert (tmp_path / "started").exists()
        process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        assert process.poll() is None
        assert not (tmp_path / "completed").exists()
        assert not (tmp_path / "unexpected-claim").exists()
        (tmp_path / "release").touch()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == ["succeeded", "todo"]
        assert (tmp_path / "completed").exists()
        assert not (tmp_path / "unexpected-claim").exists()
    finally:
        (tmp_path / "release").touch()
        if process.poll() is None:
            process.kill()  # Only this test-owned in-memory child, never a deployed worker.
            process.communicate(timeout=5)
