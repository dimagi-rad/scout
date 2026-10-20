"""Fitness test: the production worker's job concurrency fits its connection caps."""

import re

from mcp_server.services.pool import _MAX_POOLS
from tests.kamal_config import load_config


def _worker_concurrency() -> int:
    cmd = load_config("deploy-worker.yml")["servers"]["web"]["cmd"]
    match = re.search(r"--concurrency[ =](\d+)", cmd)
    return int(match.group(1)) if match else 1


def test_worker_concurrency_fits_managed_pool_cap():
    # Each job's data thread may hold one async_to_sync loop's managed pool while
    # the worker's own loop holds another; past the cap a job waits, then times out.
    assert _worker_concurrency() + 1 <= _MAX_POOLS


def test_worker_runs_more_than_one_job():
    assert _worker_concurrency() == 3
