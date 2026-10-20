"""Fitness test: the worker's job concurrency fits its connection caps."""

import re
from pathlib import Path

import pytest
from django.conf import settings

from apps.workspaces.services.materialize import _SOURCE_LOAD_EXECUTOR
from mcp_server.services.pool import _MAX_POOLS
from tests.kamal_config import load_config

REPO = Path(__file__).resolve().parent.parent


def _concurrency(cmd: str) -> int:
    match = re.search(r"--concurrency[ =](\d+)", cmd)
    return int(match.group(1)) if match else 1


def _deploy_worker_cmd() -> str:
    return load_config("deploy-worker.yml")["servers"]["web"]["cmd"]


def _line_with(path: Path, marker: str) -> str:
    lines = [line for line in path.read_text().splitlines() if marker in line]
    assert len(lines) == 1, f"expected one worker command in {path.name}, found {lines}"
    return lines[0]


def _procfile_worker_cmd() -> str:
    return _line_with(REPO / "Procfile.dev", "worker:")


def _invoke_worker_cmd() -> str:
    return _line_with(REPO / "tasks.py", "procrastinate worker")


WORKER_CMDS = pytest.mark.parametrize(
    "read_cmd",
    [_deploy_worker_cmd, _procfile_worker_cmd, _invoke_worker_cmd],
    ids=["deploy-worker.yml", "Procfile.dev", "tasks.py"],
)


@WORKER_CMDS
def test_worker_concurrency_fits_managed_pool_cap(read_cmd):
    # The worker's own loop plus one async_to_sync loop per job, plus one spare
    # slot for a loop stopped without closing; past the cap a job times out.
    assert _concurrency(read_cmd()) + 2 <= _MAX_POOLS


@WORKER_CMDS
def test_worker_runs_more_than_one_job(read_cmd):
    assert _concurrency(read_cmd()) >= 2


@WORKER_CMDS
def test_worker_load_lanes_fit_the_source_load_pool(read_cmd):
    # Each job's own lane plus the shared slots (#927). Past the pool size a load that
    # has its lane would wait for a thread while holding the run's locks.
    lanes = _concurrency(read_cmd()) + settings.MATERIALIZATION_SHARED_LOAD_SLOTS
    assert lanes <= _SOURCE_LOAD_EXECUTOR._max_workers
