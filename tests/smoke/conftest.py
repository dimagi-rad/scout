"""Smoke test configuration and shared fixtures.

Reads test targets from tests/smoke/.env. Tests skip gracefully when
their required env vars are not configured.
"""

from __future__ import annotations

import pathlib

import environ
import pytest

# Load smoke-specific .env (not the main project .env)
_smoke_dir = pathlib.Path(__file__).parent
_env_file = _smoke_dir / ".env"

smoke_env = environ.Env()
if _env_file.exists():
    smoke_env.read_env(str(_env_file))


def _csv_list(key: str) -> list[str]:
    """Read a comma-separated env var into a list of non-empty strings."""
    raw = smoke_env(key, default="")
    return [v.strip() for v in raw.split(",") if v.strip()]


@pytest.fixture(params=_csv_list("CONNECT_OPPORTUNITY_IDS") or [None])
def connect_opportunity_id(request):
    """Yield each configured Connect opportunity ID, or skip if none."""
    if request.param is None:
        pytest.skip("CONNECT_OPPORTUNITY_IDS not set in tests/smoke/.env")
    return request.param
