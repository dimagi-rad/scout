"""Fitness test: the worker's job concurrency fits its connection caps."""

import re
from pathlib import Path

import pytest

from mcp_server.services.pool import _MAX_POOLS
from tests.kamal_config import load_config

PROCFILE_DEV = Path(__file__).resolve().parent.parent / "Procfile.dev"


def _concurrency(cmd: str) -> int:
    match = re.search(r"--concurrency[ =](\d+)", cmd)
    return int(match.group(1)) if match else 1


def _deploy_worker_cmd() -> str:
    return load_config("deploy-worker.yml")["servers"]["web"]["cmd"]


def _dev_worker_cmd() -> str:
    (line,) = [line for line in PROCFILE_DEV.read_text().splitlines() if line.startswith("worker:")]
    return line


@pytest.mark.parametrize("read_cmd", [_deploy_worker_cmd, _dev_worker_cmd])
def test_worker_concurrency_fits_managed_pool_cap(read_cmd):
    # The worker's own loop plus one async_to_sync loop per job, plus one spare
    # slot for a loop stopped without closing; past the cap a job times out.
    assert _concurrency(read_cmd()) + 2 <= _MAX_POOLS


@pytest.mark.parametrize("read_cmd", [_deploy_worker_cmd, _dev_worker_cmd])
def test_worker_runs_more_than_one_job(read_cmd):
    assert _concurrency(read_cmd()) >= 2
