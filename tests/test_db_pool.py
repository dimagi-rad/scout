"""Django's psycopg connection pool (#934): opt-in wiring and per-return session reset."""

import importlib
import sys

import pytest
from django.db import connection
from django.db.backends.postgresql.base import DatabaseWrapper

from apps.common.db_pool import pool_options, reset_pooled_connection
from tests.production_settings import PRODUCTION_SETTINGS

BASE_SETTINGS = "config.settings.base"


@pytest.fixture
def load_production_with_env(monkeypatch):
    """Import production settings afresh, base included, so its env reads rerun."""
    monkeypatch.setenv("MCP_SHARED_SECRET", "test-secret")

    def _load(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        sys.modules.pop(PRODUCTION_SETTINGS, None)
        sys.modules.pop(BASE_SETTINGS, None)
        return importlib.import_module(PRODUCTION_SETTINGS)

    yield _load
    sys.modules.pop(PRODUCTION_SETTINGS, None)
    sys.modules.pop(BASE_SETTINGS, None)


@pytest.fixture
def pooled_wrapper(db):
    """A second Django connection to the test database, pooled to one connection."""
    settings_dict = {
        **connection.settings_dict,
        "OPTIONS": {
            **connection.settings_dict.get("OPTIONS", {}),
            "pool": pool_options(min_size=0, max_size=1, timeout=5),
        },
    }
    wrapper = DatabaseWrapper(settings_dict, alias="pool_under_test")
    yield wrapper
    wrapper.close()
    wrapper.close_pool()


def test_pool_is_off_by_default(load_production_with_env, monkeypatch):
    monkeypatch.delenv("DJANGO_DB_POOL_MAX_SIZE", raising=False)
    settings = load_production_with_env()
    assert "pool" not in settings.DATABASES["default"].get("OPTIONS", {})


def test_pool_size_comes_from_the_environment(load_production_with_env):
    settings = load_production_with_env(
        DJANGO_DB_POOL_MAX_SIZE="8", DJANGO_DB_POOL_MIN_SIZE="2", DJANGO_DB_POOL_TIMEOUT_S="3"
    )
    pool = settings.DATABASES["default"]["OPTIONS"]["pool"]
    assert (pool["min_size"], pool["max_size"], pool["timeout"]) == (2, 8, 3.0)
    assert pool["reset"] is reset_pooled_connection
    # Django refuses to pool persistent connections.
    assert settings.DATABASES["default"].get("CONN_MAX_AGE", 0) == 0


@pytest.mark.parametrize(
    ("min_size", "max_size", "timeout"),
    [(0, 0, 1.0), (3, 2, 1.0), (-1, 2, 1.0), (1, 2, 0.0)],
)
def test_pool_options_reject_unusable_sizes(min_size, max_size, timeout):
    with pytest.raises(ValueError, match="DJANGO_DB_POOL_"):
        pool_options(min_size=min_size, max_size=max_size, timeout=timeout)


def _session_state(cursor):
    cursor.execute(
        "SELECT pg_backend_pid(), current_setting('statement_timeout'),"
        " current_setting('search_path'), current_setting('TimeZone'),"
        " (SELECT count(*) FROM pg_locks"
        "  WHERE locktype = 'advisory' AND pid = pg_backend_pid())"
    )
    return cursor.fetchone()


@pytest.mark.django_db(transaction=True)
def test_a_returned_connection_comes_back_as_a_fresh_session(pooled_wrapper):
    pooled_wrapper.connect()
    with pooled_wrapper.cursor() as cursor:
        fresh_pid, *fresh_state = _session_state(cursor)
        cursor.execute("SET statement_timeout TO '30s'")
        cursor.execute("SET search_path TO pg_catalog")
        cursor.execute("SET TIME ZONE 'America/New_York'")
        cursor.execute("SELECT pg_advisory_lock(934, 1)")
    pooled_wrapper.close()

    pooled_wrapper.connect()
    with pooled_wrapper.cursor() as cursor:
        pid, *state = _session_state(cursor)

    assert pid == fresh_pid, "max_size=1 must reuse the one pooled connection"
    assert state == fresh_state
    assert state[2] == "UTC"
    assert state[3] == 0
