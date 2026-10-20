"""Django's psycopg connection pool, made safe for code written against fresh connections.

Before the pool (#934), ``connection.close()`` ended the Postgres session, and code
relies on that: the Cube validator slot (``cube_schema``) and view builds
(``schema_manager``) take session advisory locks on Django's connection and lean on
the close to drop them. Django sets no pool ``reset``, so a pooled connection would
carry such state into the next request.

Settings imports this module, so it must not import Django's ORM-facing apps; the
capacity tagging lives in the ``apps.common.pooled_postgresql`` backend.
"""

from typing import Any

import psycopg
from django.db import DEFAULT_DB_ALIAS, connections
from psycopg import sql

POOLED_ENGINE = "apps.common.pooled_postgresql"


class RefusalRecordingConnection(psycopg.Connection):
    """Remembers the pool's last connect failure, which never reaches the caller.

    The pool opens connections on its background worker, so a ``too many clients``
    refusal only shows up as a later checkout timeout; the backend reads this to
    tell "the server is full" from "the database is down".
    """

    # (sqlstate, message), not the exception: its traceback holds the connect kwargs.
    last_connect_failure: tuple[str | None, str] | None = None

    @classmethod
    def connect(cls, *args, **kwargs):
        try:
            conn = super().connect(*args, **kwargs)
        except BaseException as exc:
            RefusalRecordingConnection.last_connect_failure = (
                getattr(exc, "sqlstate", None),
                str(exc),
            )
            raise
        RefusalRecordingConnection.last_connect_failure = None
        return conn


def reset_pooled_connection(conn) -> None:
    """Return ``conn`` to a fresh session: settings, role, advisory locks, temp tables."""
    # DISCARD ALL refuses to run in a transaction block; the pool hands us an idle one.
    conn.autocommit = True
    conn.execute("DISCARD ALL")
    # DISCARD ALL also resets TimeZone, which Django sets only when the pool opens
    # a connection, never on checkout. OPTIONS["assume_role"] would vanish the same
    # way; restore it here before using one.
    timezone_name = connections[DEFAULT_DB_ALIAS].timezone_name
    conn.execute(sql.SQL("SET TIME ZONE {}").format(sql.Literal(timezone_name)))


def pool_options(*, min_size: int, max_size: int, timeout: float) -> dict[str, Any]:
    """``DATABASES[...]["OPTIONS"]["pool"]`` for one process's Django connection pool."""
    if max_size < 1:
        raise ValueError("DJANGO_DB_POOL_MAX_SIZE must be >= 1 to enable the pool")
    if not 0 <= min_size <= max_size:
        raise ValueError("DJANGO_DB_POOL_MIN_SIZE must be between 0 and DJANGO_DB_POOL_MAX_SIZE")
    if timeout <= 0:
        raise ValueError("DJANGO_DB_POOL_TIMEOUT_S must be > 0")
    return {
        "min_size": min_size,
        "max_size": max_size,
        "timeout": timeout,
        "reset": reset_pooled_connection,
        "connection_class": RefusalRecordingConnection,
    }


def pooled_database(db: dict[str, Any], **options) -> dict[str, Any]:
    """``db`` (a ``DATABASES`` entry) pooled, health-checked and capacity-tagged."""
    return {
        **db,
        "ENGINE": POOLED_ENGINE,
        # Django refuses to pool persistent connections.
        "CONN_MAX_AGE": 0,
        # Without it the pool hands out connections that died idle (an RDS reboot or
        # failover), and Django skips its own per-request check when pooling.
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {**db.get("OPTIONS", {}), "pool": pool_options(**options)},
    }
