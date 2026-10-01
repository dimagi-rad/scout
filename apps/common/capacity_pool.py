"""Shared "full pool vs database down" tagging for psycopg pools.

A checkout timeout is capacity when the pool is at ``max_size`` (every slot held
by a slow client) or when the last connect attempt was refused for lack of server
slots. The libpq FATAL is raised on the pool's background worker, never to the
caller, so ``_connect`` records it. A timeout on a pool that has room but cannot
fill it for any other reason means the database is unreachable, which retrying
would not fix. A cold ``open()`` timeout follows the same rule minus the
full-pool case: an unopened pool holds no slots.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool, PoolTimeout

from apps.common.capacity import classify_capacity_error


class CapacityTaggingPool(AsyncConnectionPool):
    """Raises ``exhausted_error`` (a tagged ``PoolTimeout``) when a timeout means "busy"."""

    exhausted_error: type[PoolTimeout]

    _last_connect_error: BaseException | None = None

    async def _connect(self, timeout: float | None = None):  # noqa: ASYNC109 -- psycopg_pool signature
        try:
            conn = await super()._connect(timeout)
        except BaseException as exc:
            self._last_connect_error = exc
            raise
        self._last_connect_error = None
        return conn

    def _refused_for_capacity(self) -> bool:
        last = self._last_connect_error
        return last is not None and classify_capacity_error(last) is not None

    async def open(self, wait: bool = False, timeout: float = 30.0) -> None:  # noqa: ASYNC109 -- psycopg_pool signature
        try:
            await super().open(wait, timeout)
        except PoolTimeout as exc:
            if self._refused_for_capacity():
                raise self.exhausted_error(str(exc)) from exc
            raise

    async def getconn(self, timeout: float | None = None):  # noqa: ASYNC109 -- psycopg_pool signature
        try:
            conn = await super().getconn(timeout)
        except PoolTimeout as exc:
            full = self.get_stats().get("pool_size", 0) >= self.max_size
            if full or self._refused_for_capacity():
                raise self.exhausted_error(str(exc)) from exc
            raise
        self._last_connect_error = None
        return conn
