"""Migrations must finish before a rolling deploy starts schema-dependent roles."""

import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from tests.kamal_config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("destination", [None, "staging"])
def test_api_has_an_actual_container_health_gate_before_other_backend_roles(destination):
    config = load_config("deploy.yml", destination=destination)
    web = config["servers"]["web"]
    assert web["proxy"] is False
    assert web["cmd"].split()[0] == "uvicorn"
    options = web["options"]
    assert options["health-cmd"] == "sh scripts/api-healthcheck.sh"
    assert options["health-interval"] == "5s"
    assert options["health-timeout"] == "5s"
    assert options["health-start-period"] == "120s"
    assert options["health-retries"] == 3
    assert config["deploy_timeout"] == 180
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    final_stage = re.split(r"^FROM\s+", dockerfile, flags=re.MULTILINE)[-1]
    assert re.search(r"^\s*curl\s+\\$", final_stage, re.MULTILINE), "Final image must install curl"
    assert "apt-get install" in final_stage

    filename = "deploy-staging.yml" if destination else "deploy.yml"
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / filename).read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Deploy Cube") < names.index("Drain old workers") < names.index("Deploy API")
    assert names.index("Deploy API") < names.index("Deploy MCP") < names.index("Deploy Worker")
    # GitHub's default success() gate must not be bypassed after an API failure.
    for step in steps:
        if step.get("name") in {"Drain old workers", "Deploy API", "Deploy MCP", "Deploy Worker"}:
            assert not step.get("continue-on-error", False)
            assert step.get("if", "success()") == "success()"
    drain = next(step for step in steps if step.get("name") == "Drain old workers")
    target = "staging" if destination else "production"
    assert f"bash -s -- {target} 600 < scripts/drain-workers.sh" in drain["run"]
    assert '"scout@$SCOUT_EC2_IP"' in drain["run"]
    assert drain["timeout-minutes"] == 12


@pytest.fixture
def entrypoint_commands(tmp_path):
    """Run the real entrypoint; fake only commands it would start externally."""
    log = tmp_path / "commands.log"
    script = (
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "name = Path(sys.argv[0]).name\n"
        "with Path(os.environ['CALL_LOG']).open('a') as output:\n"
        "    time.sleep(float(os.environ.get('CALL_LOG_WRITE_DELAY', '0')))\n"
        "    output.write(name + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "if name == 'python' and sys.argv[1:3] == ['manage.py', 'migrate']:\n"
        "    release = os.environ.get('MIGRATION_RELEASE_FILE')\n"
        "    while release and not Path(release).exists():\n"
        "        time.sleep(0.01)\n"
        "    sys.exit(int(os.environ.get('MIGRATION_EXIT_CODE', '0')))\n"
        "if name == 'uvicorn':\n"
        "    sys.exit(17)\n"
        "if name == 'curl':\n"
        "    sys.exit(int(os.environ.get('CURL_EXIT_CODE', '0')))\n"
    )
    for name in ("python", "uvicorn", "curl"):
        command = tmp_path / name
        command.write_text(script)
        command.chmod(0o755)
    return log, {
        "PATH": os.pathsep.join(
            (str(tmp_path), *(part for part in os.defpath.split(os.pathsep) if part))
        ),
        "CALL_LOG": str(log),
        "DJANGO_ALLOWED_HOSTS": "scout-staging.example.invalid,other.example.invalid",
    }


def test_fake_command_path_never_includes_current_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "defpath", ":/bin::/usr/bin:")
    _, env = entrypoint_commands.__wrapped__(tmp_path)
    assert env["PATH"].split(os.pathsep) == [str(tmp_path), "/bin", "/usr/bin"]


def _entrypoint(*args):
    return ["/bin/bash", str(REPO_ROOT / "docker-entrypoint.sh"), *args]


def test_failed_migration_never_executes_oauth_sync_or_api_server(entrypoint_commands):
    log, env = entrypoint_commands
    result = subprocess.run(  # noqa: S603 - repository entrypoint and isolated fake PATH
        _entrypoint("uvicorn", "config.asgi:application"),
        env={**env, "MIGRATION_EXIT_CODE": "23"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 23
    assert log.read_text().splitlines() == ["python manage.py migrate --no-input"]


@contextmanager
def _held_api_process(env, release):
    """Own the fake process group, including cleanup after a failed assertion."""
    process = subprocess.Popen(  # noqa: S603 - repository entrypoint and isolated fake PATH
        _entrypoint("uvicorn", "config.asgi:application", "--port", "8000"),
        env={**env, "MIGRATION_RELEASE_FILE": str(release)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        # Let the fake migration finish before waiting on its parent shell.
        # Signals below can target only this newly created synthetic group.
        release.touch()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)


def _wait_for_migration_log(log, process):
    deadline = time.monotonic() + 5
    lines = []
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        if lines:
            break
        assert process.poll() is None, "The fake entrypoint exited before recording the migration"
        time.sleep(0.01)
    assert lines == ["python manage.py migrate --no-input"], f"Unexpected migration log: {lines}"


@pytest.mark.parametrize("write_delay", [0, 0.15])
def test_slow_migration_cannot_start_api_until_it_finishes(
    entrypoint_commands, tmp_path, write_delay
):
    log, env = entrypoint_commands
    release = tmp_path / "migration-complete"
    with _held_api_process({**env, "CALL_LOG_WRITE_DELAY": str(write_delay)}, release) as process:
        _wait_for_migration_log(log, process)
        assert process.poll() is None
        release.touch()
        process.communicate(timeout=5)
        assert process.returncode == 17  # The final command's status is preserved.
        assert log.read_text().splitlines() == [
            "python manage.py migrate --no-input",
            "python manage.py setup_oauth_apps --domain scout-staging.example.invalid",
            "uvicorn config.asgi:application --port 8000",
        ]


def test_failed_assertion_releases_and_reaps_the_held_fake_migration(entrypoint_commands, tmp_path):
    log, env = entrypoint_commands
    release = tmp_path / "migration-complete"
    with pytest.raises(AssertionError, match="Synthetic mid-test assertion"):
        with _held_api_process(env, release) as process:
            _wait_for_migration_log(log, process)
            assert not release.exists()
            raise AssertionError("Synthetic mid-test assertion")
    assert release.exists()
    assert process.returncode == 17  # Released normally; no orphan or forced shutdown.
    assert log.read_text().splitlines()[-1] == "uvicorn config.asgi:application --port 8000"


@pytest.mark.parametrize(
    "args",
    [("python", "-m", "mcp_server.server"), ("python", "manage.py", "procrastinate", "worker")],
)
def test_non_api_roles_do_not_race_the_api_by_running_migrations(entrypoint_commands, args):
    log, env = entrypoint_commands
    result = subprocess.run(  # noqa: S603 - repository entrypoint and isolated fake PATH
        _entrypoint(*args), env=env, capture_output=True, text=True, timeout=5, check=False
    )
    assert result.returncode == 0
    assert log.read_text().splitlines() == [" ".join(args)]


@pytest.mark.parametrize("destination", [None, "staging"])
@pytest.mark.parametrize("curl_exit", [0, 7, 22, 28])
def test_health_gate_uses_container_local_url_correct_host_and_preserves_failure(
    entrypoint_commands, destination, curl_exit
):
    log, env = entrypoint_commands
    config = load_config("deploy.yml", destination=destination)
    allowlist = config["env"]["clear"]["DJANGO_ALLOWED_HOSTS"]
    host = allowlist.split(",")[0].strip()
    result = subprocess.run(  # noqa: S603 - fixed script; curl is an isolated recording fake
        ["/bin/sh", str(REPO_ROOT / "scripts" / "api-healthcheck.sh")],
        env={**env, "DJANGO_ALLOWED_HOSTS": allowlist, "CURL_EXIT_CODE": str(curl_exit)},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == curl_exit
    assert log.read_text().splitlines() == [
        "curl --fail --silent --show-error --max-time 4 --noproxy * "
        f"--header Host: {host} --output /dev/null http://127.0.0.1:8000/health/"
    ]


@pytest.mark.parametrize("allowed_hosts", [None, "", " \t ", ",other.example.invalid"])
def test_health_gate_does_not_guess_a_host_when_allowlist_is_empty(
    entrypoint_commands, allowed_hosts
):
    log, env = entrypoint_commands
    env = {key: value for key, value in env.items() if key != "DJANGO_ALLOWED_HOSTS"}
    if allowed_hosts is not None:
        env["DJANGO_ALLOWED_HOSTS"] = allowed_hosts
    result = subprocess.run(  # noqa: S603 - fixed script and synthetic empty allowlist
        ["/bin/sh", str(REPO_ROOT / "scripts" / "api-healthcheck.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "DJANGO_ALLOWED_HOSTS must contain the API host" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    "allowlist, expected",
    [
        ("scout.example.invalid,other.example.invalid", "scout.example.invalid"),
        (" \tscout.example.invalid \t, other.example.invalid", "scout.example.invalid"),
    ],
)
def test_health_gate_uses_only_the_trimmed_first_allowed_host(
    entrypoint_commands, allowlist, expected
):
    log, env = entrypoint_commands
    result = subprocess.run(  # noqa: S603 - only an isolated curl recorder can execute
        ["/bin/sh", str(REPO_ROOT / "scripts" / "api-healthcheck.sh")],
        env={**env, "DJANGO_ALLOWED_HOSTS": allowlist},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"--header Host: {expected} --output" in log.read_text()
    assert "other.example.invalid" not in log.read_text()


@pytest.mark.parametrize(
    "allowlist", ["scout .example.invalid", "scout.example.invalid\nInjected: header"]
)
def test_health_gate_rejects_internal_whitespace_without_joining_or_forwarding_it(
    entrypoint_commands, allowlist
):
    log, env = entrypoint_commands
    result = subprocess.run(  # noqa: S603 - fixed script and isolated curl recorder
        ["/bin/sh", str(REPO_ROOT / "scripts" / "api-healthcheck.sh")],
        env={**env, "DJANGO_ALLOWED_HOSTS": allowlist},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert "must not contain internal whitespace" in result.stderr
    assert not log.exists()
