"""Smoke test configuration and shared fixtures.

Reads test targets from tests/smoke/.env. Tests skip gracefully when
their required env vars are not configured.

Run smoke tests against the real platform database:

    uv run pytest -m smoke --override-ini="addopts=" \
        -o "DJANGO_SETTINGS_MODULE=config.settings.development" \
        -s --log-cli-level=INFO
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


@pytest.fixture(scope="session")
def django_db_setup():
    """Skip test database creation — smoke tests use the real platform DB."""
    pass


@pytest.fixture(scope="session")
def scout_base_url():
    """Base URL for Scout's Django backend."""
    return smoke_env("SCOUT_BASE_URL", default="http://localhost:8001")


@pytest.fixture(params=_csv_list("CONNECT_OPPORTUNITY_IDS") or [None])
def connect_opportunity_id(request):
    """Yield each configured Connect opportunity ID, or skip if none."""
    if request.param is None:
        pytest.skip("CONNECT_OPPORTUNITY_IDS not set in tests/smoke/.env")
    return request.param
