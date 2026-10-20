"""Django's PostgreSQL backend whose pool checkout timeouts say whether Scout is busy.

Django raises the pool's bare ``PoolTimeout``, which ``apps.common.capacity`` cannot
tell from an outage, so a full pool would 500 and fail the health probe instead of
answering "busy" (#934). Same rule as ``apps.common.capacity_pool`` for the
checkpointer: full pool or a refused connect is capacity, anything else is not.
"""

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
            conn = super().get_new_connection(conn_params)
        except PoolTimeout as exc:
            if self._pool_is_busy():
                raise DjangoPoolExhausted(str(exc)) from exc
            raise
        # A served checkout retires an old refusal, or a later outage would read as busy.
        RefusalRecordingConnection.last_connect_failure = None
        return conn

    def _pool_is_busy(self) -> bool:
        # A recorded connect failure decides first: pool_size counts connections
        # created, not healthy ones, so a pool that grew under load reads as full
        # through an outage. Served checkouts clear it, so real busy never vetoes.
        failure = RefusalRecordingConnection.last_connect_failure
        if failure is not None:
            sqlstate, message = failure
            return sqlstate == TOO_MANY_CONNECTIONS_SQLSTATE or is_capacity_message(message)
        return self.pool.get_stats().get("pool_size", 0) >= self.pool.max_size
