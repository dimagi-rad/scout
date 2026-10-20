"""Django's PostgreSQL backend whose pool checkout timeouts say whether Scout is busy.

Django raises the pool's bare ``PoolTimeout``, which ``apps.common.capacity`` cannot
tell from an outage, so a full pool would 500 and fail the health probe instead of
answering "busy" (#934). A recent connect failure decides: a refusal for lack of
slots is capacity, anything else an outage. Without one, a full pool is capacity.
``apps.common.capacity_pool`` (the checkpointer) still lets a full pool win.
"""

import time

from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper
from psycopg_pool import PoolTimeout

from apps.common.capacity import (
    TOO_MANY_CONNECTIONS_SQLSTATE,
    CapacityResource,
    is_capacity_message,
)
from apps.common.db_pool import RefusalRecordingConnection


class DjangoPoolExhausted(PoolTimeout):
    """No Django connection came free, or the server refused new ones, within the timeout."""

    capacity_resource = CapacityResource.DATABASE


class DatabaseWrapper(PostgresDatabaseWrapper):
    def get_new_connection(self, conn_params):
        try:
            return super().get_new_connection(conn_params)
        except PoolTimeout as exc:
            if self._pool_is_busy():
                raise DjangoPoolExhausted(str(exc)) from exc
            raise

    def _pool_is_busy(self) -> bool:
        # A recent connect failure decides first: pool_size counts connections
        # created, not healthy ones, so a pool that grew under load reads as full
        # through an outage. "Recent" spans the pool's whole retry budget, whose
        # backoff gaps between attempts grow past a minute; older ones expire.
        failure = RefusalRecordingConnection.last_connect_failure
        if failure is not None:
            failed_at, sqlstate, message = failure
            if time.monotonic() - failed_at <= self.pool.reconnect_timeout:
                return sqlstate == TOO_MANY_CONNECTIONS_SQLSTATE or is_capacity_message(message)
        return self.pool.get_stats().get("pool_size", 0) >= self.pool.max_size
