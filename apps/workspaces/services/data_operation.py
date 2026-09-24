"""Serialize recovery and materialization through semantic publication.

Lock order for user-triggered tenant writers (D1): request intent is captured
before any wait, then workspace ``W`` → sorted tenant ``T*`` → own view build →
Cube. Standalone refresh and retirement hold ``T`` only and never take another
workspace's ``W`` while holding it; sibling rebuilds are deferred tasks.

A ``T`` region must not fan out into work that takes ``W``: child tasks inherit
the held-tenant context, and ``W`` is refused whenever it shows tenant keys.
Threads that may enter a lock region must be started with ``run_data_thread``,
which is what lets them reuse their task's locks. The guard against other thread
starts is best-effort: it catches context-copying ones (``asyncio.to_thread``,
``sync_to_async``) but cannot see a plain ``threading.Thread``.
"""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps

import psycopg
import psycopg.errors
from django.db import connections

_LOCK_NAMESPACE = 0x53434441
_TENANT_LOCK_NAMESPACE = 0x5343544E
_LOCK_TIMEOUT = "30min"
_held_workspaces = ContextVar("scout_data_workspaces", default=(None, frozenset()))
_held_tenants = ContextVar("scout_data_tenants", default=(None, frozenset()))
_data_thread_owner = ContextVar("scout_data_thread_owner", default=None)


class DataLockTimeout(Exception):
    """A data lock was not granted within ``_LOCK_TIMEOUT``; never continue silently."""


_EXPAND_TENANTS = "Cannot expand held tenant locks; collect every tenant before acquiring"
_WORKSPACE_AFTER_TENANT = "Cannot acquire a workspace lock while tenant locks are held"
_UNBRIDGED_THREAD = (
    "A thread entered its task's data-lock region without run_data_thread; it would wait "
    "on locks its own task holds"
)


class LockOrderError(RuntimeError):
    """A nested tenant-lock request would add keys while others are already held.

    Reentrancy only covers a subset of held keys. Expanding the held set could
    acquire a lower key after a higher one and reverse the global order, so the
    caller must collect the complete tenant set before acquiring.
    """


async def run_data_thread(function, /, *args, **kwargs):
    """Keep owning locks until a sync mutation has actually stopped.

    Cancelling ``to_thread`` only cancels its waiter, not the running thread.
    Drain a shielded thread before propagating cancellation so another repair
    cannot overlap its still-running load or Cube publication.
    """
    owner = asyncio.current_task()

    def invoke():
        token = _data_thread_owner.set((owner, threading.get_ident()))
        try:
            return function(*args, **kwargs)
        finally:
            _data_thread_owner.reset(token)

    work = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # Repeated worker-abort requests must not release the lock early.
                continue
            except Exception:
                break
        if not work.cancelled():
            work.exception()  # Retrieve any failure while preserving cancellation.
        raise


def _lock_key(value) -> int:
    return int.from_bytes(hashlib.sha256(str(value).encode()).digest()[:4], signed=True)


def tenant_lock_key(tenant_id) -> int:
    """Physical advisory key for one tenant; a reduced keyspace, so keys may collide."""
    return _lock_key(tenant_id)


def tenant_lock_keys(tenant_ids) -> tuple[int, ...]:
    """Sorted, deduplicated physical keys — the order every writer acquires in.

    Sorting the hashed keys rather than the UUIDs means two tenants that collide
    on one key are acquired once, and no pair of writers can take the same two
    keys in opposite physical order.
    """
    return tuple(sorted({tenant_lock_key(tenant_id) for tenant_id in tenant_ids}))


def _connection_params() -> dict:
    params = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    return params


def _sync_lock_owner():
    bridge = _data_thread_owner.get()
    if bridge is not None and bridge[1] == threading.get_ident():
        return bridge[0]
    return threading.current_thread()


def assert_tenant_lock_held(tenant_id) -> None:
    """Raise LockOrderError unless the calling task (or its data thread) holds T.

    For code whose safety rests on T rather than on a row lock: a row lock is
    released at commit, while T spans a writer's whole load.
    """
    owner, keys = _held_tenants.get()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        current = _sync_lock_owner()
    else:
        current = asyncio.current_task()
    # A thread reached without run_data_thread sees its task's locks but not its
    # ownership; say so rather than claiming T is not held.
    _refuse_unbridged_thread(current, owner, keys)
    if owner is None or owner is not current or tenant_lock_key(tenant_id) not in keys:
        raise LockOrderError("This candidate operation requires holding the tenant lock T")


def _refuse_unbridged_thread(owner, inherited_owner, inherited) -> None:
    # A bare asyncio.to_thread copies the task's context but not its ownership,
    # so it would open a second session and wait out _LOCK_TIMEOUT on a lock
    # its own task holds. That is always a bug, never a concurrent writer.
    if (
        inherited
        and isinstance(inherited_owner, asyncio.Task)
        and isinstance(owner, threading.Thread)
    ):
        raise LockOrderError(_UNBRIDGED_THREAD)


@contextmanager
def sync_workspace_data_lock(workspace_id):
    """Reuse only the originating task's drained thread, otherwise acquire W."""
    key = str(workspace_id)
    owner = _sync_lock_owner()
    inherited_owner, inherited = _held_workspaces.get()
    _refuse_unbridged_thread(owner, inherited_owner, inherited)
    held = inherited if inherited_owner is owner else frozenset()
    if key in held:
        yield
        return
    _tenant_owner, tenant_keys = _held_tenants.get()
    if tenant_keys:
        # The order inversion is the root cause even for a bridged thread.
        raise LockOrderError(_WORKSPACE_AFTER_TENANT)
    # Nested W is single-workspace by construction: every caller locks the one
    # workspace it is working on, so no ordering rule is needed across W keys.
    with psycopg.connect(**_connection_params(), autocommit=True) as conn:
        conn.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        _sync_acquire(conn, _LOCK_NAMESPACE, _lock_key(key))
        token = _held_workspaces.set((owner, held | {key}))
        try:
            yield
        finally:
            _held_workspaces.reset(token)


# statement_timeout (57014) can arrive from DATABASE_URL options or a role
# default; either deadline means the lock was not granted.
_LOCK_DEADLINE_ERRORS = (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled)


def _lock_timeout(namespace: int, key: int) -> DataLockTimeout:
    return DataLockTimeout(f"Timed out waiting for data lock {namespace:#x}/{key}")


def _sync_acquire(conn, namespace, key):
    try:
        conn.execute("SELECT pg_advisory_lock(%s, %s)", (namespace, key))
    except _LOCK_DEADLINE_ERRORS as exc:
        raise _lock_timeout(namespace, key) from exc


@contextmanager
def sync_tenant_data_lock(tenant_ids):
    """Synchronous T acquisition with the same physical ordering and ownership rules."""
    keys = tenant_lock_keys(tenant_ids)
    owner = _sync_lock_owner()
    inherited_owner, inherited = _held_tenants.get()
    _refuse_unbridged_thread(owner, inherited_owner, inherited)
    held = inherited if inherited_owner is owner else frozenset()
    if held:
        if set(keys) <= held:
            yield
            return
        raise LockOrderError(_EXPAND_TENANTS)
    if not keys:
        yield
        return
    with psycopg.connect(**_connection_params(), autocommit=True) as conn:
        conn.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        for key in keys:
            _sync_acquire(conn, _TENANT_LOCK_NAMESPACE, key)
        token = _held_tenants.set((owner, frozenset(keys)))
        try:
            yield
        finally:
            _held_tenants.reset(token)


async def _acquire(conn, namespace: int, key: int) -> None:
    try:
        await conn.execute("SELECT pg_advisory_lock(%s, %s)", (namespace, key))
    except _LOCK_DEADLINE_ERRORS as exc:
        raise _lock_timeout(namespace, key) from exc


@asynccontextmanager
async def workspace_data_lock(workspace_id):
    key = str(workspace_id)
    owner, inherited = _held_workspaces.get()
    task = asyncio.current_task()
    held = inherited if owner is task else frozenset()
    if key in held:
        yield
        return
    _tenant_owner, tenant_keys = _held_tenants.get()
    if tenant_keys:
        # Includes child tasks of a T holder: they inherit the context, and a
        # W they took would wait on the parent's order from a third session.
        raise LockOrderError(_WORKSPACE_AFTER_TENANT)
    lock_key = _lock_key(key)
    # A dedicated session keeps the lock across awaits and thread-based pipeline
    # work. Closing it also releases the lock if a worker is cancelled or dies.
    async with await psycopg.AsyncConnection.connect(
        **_connection_params(), autocommit=True
    ) as conn:
        await conn.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        await _acquire(conn, _LOCK_NAMESPACE, lock_key)
        token = _held_workspaces.set((task, held | {key}))
        try:
            yield
        finally:
            _held_workspaces.reset(token)


@asynccontextmanager
async def tenant_data_lock(tenant_ids):
    """Hold the tenant writer locks for ``tenant_ids`` in global key order.

    Same-task reentrancy reuses an already-held subset; child tasks inherit the
    context variable but not ownership, so they open their own session and wait.
    """
    keys = tenant_lock_keys(tenant_ids)
    owner, inherited = _held_tenants.get()
    task = asyncio.current_task()
    held = inherited if owner is task else frozenset()
    if held:
        if set(keys) <= held:
            yield
            return
        raise LockOrderError(_EXPAND_TENANTS)
    if not keys:
        yield
        return
    async with await psycopg.AsyncConnection.connect(
        **_connection_params(), autocommit=True
    ) as conn:
        await conn.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
        for key in keys:
            await _acquire(conn, _TENANT_LOCK_NAMESPACE, key)
        token = _held_tenants.set((task, frozenset(keys)))
        try:
            yield
        finally:
            _held_tenants.reset(token)


@asynccontextmanager
async def tenant_data_lock_if_free(tenant_id):
    """Take one tenant's T only if nobody holds it; yield whether it was taken.

    For sweeps: a held T means a live writer, so they skip the tenant rather
    than queue behind a load that can run for hours.
    """
    (key,) = tenant_lock_keys([tenant_id])
    owner, inherited = _held_tenants.get()
    task = asyncio.current_task()
    held = inherited if owner is task else frozenset()
    if held:
        if key in held:
            yield True
            return
        raise LockOrderError(_EXPAND_TENANTS)
    async with await psycopg.AsyncConnection.connect(
        **_connection_params(), autocommit=True
    ) as conn:
        cursor = await conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (_TENANT_LOCK_NAMESPACE, key)
        )
        (acquired,) = await cursor.fetchone()
        if not acquired:
            yield False
            return
        token = _held_tenants.set((task, frozenset({key})))
        try:
            yield True
        finally:
            _held_tenants.reset(token)


def serialized_workspace_data(function):
    @wraps(function)
    async def wrapped(workspace_id, *args, **kwargs):
        async with workspace_data_lock(workspace_id):
            return await function(workspace_id, *args, **kwargs)

    return wrapped
