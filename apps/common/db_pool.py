"""Django's psycopg connection pool, made safe for code written against fresh connections.

Before the pool (#934), ``connection.close()`` ended the Postgres session, and code
relies on that: ``SET search_path``/``statement_timeout`` in custom datasets, session
advisory locks in the Cube validator and view builds. Django sets no pool ``reset``,
so a pooled connection would carry that state into the next request.
"""

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections
from psycopg import sql


def reset_pooled_connection(conn) -> None:
    """Return ``conn`` to a fresh session: settings, role, advisory locks, temp tables."""
    # DISCARD ALL refuses to run in a transaction block; the pool hands us an idle one.
    conn.autocommit = True
    conn.execute("DISCARD ALL")
    # DISCARD ALL also resets TimeZone, which Django sets only when the pool opens
    # a connection, never on checkout.
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
    }
