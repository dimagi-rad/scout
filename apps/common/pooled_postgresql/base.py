"""Django's PostgreSQL backend whose pool checkout timeouts say whether Scout is busy.

Django raises the pool's bare ``PoolTimeout``, which ``apps.common.capacity`` cannot
tell from an outage, so a full pool would 500 and fail the health probe instead of
answering "busy" (#934). Same rule as ``apps.common.capacity_pool`` for the
checkpointer: full pool or a refused connect is capacity, anything else is not.
"""

from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper
from psycopg_pool import PoolTimeout

from apps.common.capacity import CapacityResource, classify_capacity_error
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
        if self.pool.get_stats().get("pool_size", 0) >= self.pool.max_size:
            return True
        refusal = RefusalRecordingConnection.last_connect_error
        return refusal is not None and classify_capacity_error(refusal) is not None
