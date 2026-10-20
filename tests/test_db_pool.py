"""Django's psycopg connection pool (#934): opt-in wiring, per-return reset, capacity."""

import importlib
import sys
from unittest.mock import patch

import psycopg
import pytest
from django.db import OperationalError, connection
from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper
from psycopg_pool import PoolTimeout

from apps.common.capacity import CapacityResource, classify_capacity_error
from apps.common.db_pool import (
    POOLED_ENGINE,
    RefusalRecordingConnection,
    pool_options,
    pooled_database,
    reset_pooled_connection,
)
from apps.common.pooled_postgresql.base import DatabaseWrapper, DjangoPoolExhausted
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
def no_recorded_refusal(monkeypatch):
    monkeypatch.setattr(RefusalRecordingConnection, "last_connect_error", None)


@pytest.fixture
def make_pooled_wrapper(db, no_recorded_refusal):
    """Django connections to the test database sharing one pool of ``max_size``."""
    wrappers = []

    def _make(*, max_size=1, timeout=5.0):
        settings_dict = pooled_database(
            connection.settings_dict, min_size=0, max_size=max_size, timeout=timeout
        )
        wrapper = DatabaseWrapper(settings_dict, alias="pool_under_test")
        wrappers.append(wrapper)
        return wrapper

    yield _make
    for wrapper in wrappers:
        wrapper.close()
    if wrappers:
        wrappers[0].close_pool()


def test_pool_is_off_by_default(load_production_with_env):
    # Set rather than unset: base re-reads .env, which would fill an unset value.
    settings = load_production_with_env(DJANGO_DB_POOL_MAX_SIZE="0")
    assert settings.DATABASES["default"]["ENGINE"] != POOLED_ENGINE
    assert "pool" not in settings.DATABASES["default"].get("OPTIONS", {})


def test_pool_size_comes_from_the_environment(load_production_with_env):
    settings = load_production_with_env(
        DJANGO_DB_POOL_MAX_SIZE="8", DJANGO_DB_POOL_MIN_SIZE="2", DJANGO_DB_POOL_TIMEOUT_S="3"
    )
    db = settings.DATABASES["default"]
    pool = db["OPTIONS"]["pool"]
    assert (pool["min_size"], pool["max_size"], pool["timeout"]) == (2, 8, 3.0)
    assert pool["reset"] is reset_pooled_connection
    assert pool["connection_class"] is RefusalRecordingConnection
    assert db["ENGINE"] == POOLED_ENGINE
    assert db["CONN_HEALTH_CHECKS"] is True
    # Django refuses to pool persistent connections.
    assert db.get("CONN_MAX_AGE", 0) == 0


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
def test_a_returned_connection_comes_back_as_a_fresh_session(make_pooled_wrapper):
    wrapper = make_pooled_wrapper()
    wrapper.connect()
    with wrapper.cursor() as cursor:
        fresh_pid, *fresh_state = _session_state(cursor)
        cursor.execute("SET statement_timeout TO '30s'")
        cursor.execute("SET search_path TO pg_catalog")
        cursor.execute("SET TIME ZONE 'America/New_York'")
        cursor.execute("SELECT pg_advisory_lock(934, 1)")
    wrapper.close()

    wrapper.connect()
    with wrapper.cursor() as cursor:
        pid, *state = _session_state(cursor)

    assert pid == fresh_pid, "max_size=1 must reuse the one pooled connection"
    assert state == fresh_state
    assert state[2] == "UTC"
    assert state[3] == 0


@pytest.mark.django_db(transaction=True)
def test_a_full_pool_is_busy_not_an_outage(make_pooled_wrapper):
    holder = make_pooled_wrapper(max_size=1, timeout=0.2)
    holder.connect()
    waiter = make_pooled_wrapper(max_size=1, timeout=0.2)

    with pytest.raises(OperationalError) as raised:
        waiter.ensure_connection()

    assert isinstance(raised.value.__cause__, DjangoPoolExhausted)
    assert classify_capacity_error(raised.value).resource == CapacityResource.DATABASE


def _timeout_after(last_connect_error, make_pooled_wrapper, monkeypatch):
    monkeypatch.setattr(RefusalRecordingConnection, "last_connect_error", last_connect_error)
    wrapper = make_pooled_wrapper(max_size=4)
    with (
        patch.object(
            PostgresDatabaseWrapper, "get_new_connection", side_effect=PoolTimeout("timed out")
        ),
        pytest.raises(PoolTimeout) as raised,
    ):
        wrapper.get_new_connection({})
    return raised.value


def test_a_timeout_after_a_slot_refusal_is_busy(make_pooled_wrapper, monkeypatch):
    refusal = psycopg.OperationalError("FATAL:  sorry, too many clients already")
    raised = _timeout_after(refusal, make_pooled_wrapper, monkeypatch)
    assert isinstance(raised, DjangoPoolExhausted)


def test_a_timeout_on_an_unreachable_database_stays_an_outage(make_pooled_wrapper, monkeypatch):
    down = psycopg.OperationalError("connection refused")
    raised = _timeout_after(down, make_pooled_wrapper, monkeypatch)
    assert not isinstance(raised, DjangoPoolExhausted)
    assert classify_capacity_error(raised) is None


@pytest.mark.django_db(transaction=True)
def test_the_pool_connection_records_its_last_connect_outcome(no_recorded_refusal):
    params = connection.get_connection_params()
    with pytest.raises(psycopg.OperationalError):
        RefusalRecordingConnection.connect(**{**params, "port": 1, "connect_timeout": 2})
    assert RefusalRecordingConnection.last_connect_error is not None

    RefusalRecordingConnection.connect(**params).close()
    assert RefusalRecordingConnection.last_connect_error is None
