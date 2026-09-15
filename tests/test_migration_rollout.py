"""Migrations must finish before a rolling deploy starts schema-dependent roles."""

import os
import subprocess
import sys
import time
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
    assert "curl" in (REPO_ROOT / "Dockerfile").read_text()

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
        "PATH": os.pathsep.join((str(tmp_path), os.defpath)),
        "CALL_LOG": str(log),
        "DJANGO_ALLOWED_HOSTS": "scout-staging.example.invalid,other.example.invalid",
    }


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


def test_slow_migration_cannot_start_api_until_it_finishes(entrypoint_commands, tmp_path):
    log, env = entrypoint_commands
    release = tmp_path / "migration-complete"
    process = subprocess.Popen(  # noqa: S603 - repository entrypoint and isolated fake PATH
        _entrypoint("uvicorn", "config.asgi:application", "--port", "8000"),
        env={**env, "MIGRATION_RELEASE_FILE": str(release)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert log.read_text().splitlines() == ["python manage.py migrate --no-input"]
        assert process.poll() is None
        release.touch()
        process.communicate(timeout=5)
        assert process.returncode == 17  # The final command's status is preserved.
        assert log.read_text().splitlines() == [
            "python manage.py migrate --no-input",
            "python manage.py setup_oauth_apps --domain scout-staging.example.invalid",
            "uvicorn config.asgi:application --port 8000",
        ]
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


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
    host = config["env"]["clear"]["DJANGO_ALLOWED_HOSTS"]
    result = subprocess.run(  # noqa: S603 - fixed script; curl is an isolated recording fake
        ["/bin/sh", str(REPO_ROOT / "scripts" / "api-healthcheck.sh")],
        env={**env, "DJANGO_ALLOWED_HOSTS": host, "CURL_EXIT_CODE": str(curl_exit)},
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


def test_health_gate_does_not_guess_a_host_when_allowlist_is_empty(entrypoint_commands):
    log, env = entrypoint_commands
    result = subprocess.run(  # noqa: S603 - fixed script and synthetic empty allowlist
        ["/bin/sh", str(REPO_ROOT / "scripts" / "api-healthcheck.sh")],
        env={**env, "DJANGO_ALLOWED_HOSTS": ""},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert not log.exists()
