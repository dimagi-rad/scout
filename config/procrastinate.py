"""Procrastinate app reference for background task processing.

The actual Procrastinate `App` is constructed by `procrastinate.contrib.django`
once Django is ready. This module just re-exports it so tasks can do
`from config.procrastinate import app`.
"""

import functools

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from procrastinate.contrib.django import app

__all__ = ["app", "ensure_fresh_db_connections"]


def ensure_fresh_db_connections(func):
    """Close stale or dead Django DB connections before a task body runs.

    The worker is a long-lived process with no HTTP request cycle, so Django's
    request_started/request_finished hooks never run and a connection that dies
    underneath it (RDS restart or upgrade, idle TCP timeout) is reused — closed
    — forever: every subsequent ORM call fails instantly with
    ``psycopg.OperationalError: the connection is closed``, including the
    janitor task that is supposed to rescue jobs stranded by exactly this
    (June 2026 incident: ~22h of failed background jobs after an RDS upgrade).

    ``close_old_connections()`` discards unusable/expired connections so the
    next ORM call opens a fresh one. It must run on the thread that owns them:
    ``sync_to_async``'s thread-sensitive executor — the same thread the async
    ORM routes queries through.

    Apply between ``@app.task`` and the function so procrastinate registers
    the wrapped callable. ``tests/test_worker_db_resilience.py`` enforces that
    every task in ``apps.workspaces.tasks`` carries this wrapper.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        await sync_to_async(close_old_connections)()
        return await func(*args, **kwargs)

    wrapper._ensures_fresh_db_connections = True
    return wrapper
