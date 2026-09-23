import os

import pytest

from mcp_server.services.pool import release_pools_of_finished_loops

# dbt sends anonymous usage stats to dbt Labs on every invocation. Read per
# invocation, so setting it at conftest import is early enough.
os.environ["DO_NOT_TRACK"] = "1"

# Registered from the rootdir conftest so the guard also covers apps/*/tests and
# single-file runs that never load tests/conftest.py.
pytest_plugins = ["tests.network_guard"]


@pytest.fixture(autouse=True)
def _release_managed_pools_of_finished_loops():
    """pytest-asyncio gives every async test its own loop, and ``async_to_sync`` in
    sync tests builds one per call. Their shutdown closes their managed-DB pools;
    this catches any loop that was closed without it, so dead pools can't hold
    connection slots or the process-wide pool cap for the rest of the run."""
    yield
    release_pools_of_finished_loops()
