"""Serialize recovery and materialization through semantic publication."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

import psycopg
from django.db import connections

_LOCK_NAMESPACE = 0x53434441
_held_workspaces = ContextVar("scout_data_workspaces", default=(None, frozenset()))


@asynccontextmanager
async def workspace_data_lock(workspace_id):
    key = str(workspace_id)
    owner, inherited = _held_workspaces.get()
    task = asyncio.current_task()
    held = inherited if owner is task else frozenset()
    if key in held:
        yield
        return
    params = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    lock_key = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], signed=True)
    # A dedicated session keeps the lock across awaits and thread-based pipeline
    # work. Closing it also releases the lock if a worker is cancelled or dies.
    async with await psycopg.AsyncConnection.connect(**params, autocommit=True) as conn:
        await conn.execute("SET lock_timeout = '30min'")
        await conn.execute("SELECT pg_advisory_lock(%s, %s)", (_LOCK_NAMESPACE, lock_key))
        token = _held_workspaces.set((task, held | {key}))
        try:
            yield
        finally:
            _held_workspaces.reset(token)


def serialized_workspace_data(function):
    @wraps(function)
    async def wrapped(workspace_id, *args, **kwargs):
        async with workspace_data_lock(workspace_id):
            return await function(workspace_id, *args, **kwargs)

    return wrapped
