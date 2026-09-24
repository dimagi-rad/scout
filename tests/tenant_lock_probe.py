"""Probe whether a tenant writer lock is free, from outside its holder."""

from contextlib import contextmanager

import psycopg

from apps.workspaces.services.data_operation import (
    _TENANT_LOCK_NAMESPACE,
    _connection_params,
    tenant_lock_key,
)


@contextmanager
def try_tenant_data_lock(tenant_id):
    """Yield True when this separate session could take the tenant lock.

    The session closes on exit, releasing the lock if it was taken.
    """
    key = tenant_lock_key(tenant_id)
    with psycopg.connect(**_connection_params(), autocommit=True) as conn:
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (_TENANT_LOCK_NAMESPACE, key)
        ).fetchone()[0]
        yield bool(acquired)
