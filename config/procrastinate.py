"""Procrastinate app reference for background task processing.

The actual Procrastinate `App` is constructed by `procrastinate.contrib.django`
once Django is ready. This module just re-exports it so tasks can do
`from config.procrastinate import app`.
"""

import functools

from asgiref.sync import sync_to_async
from django.db import close_old_connections, reset_queries
from procrastinate.contrib.django import app

__all__ = ["app", "task"]


def _cleanup_after() -> None:
    close_old_connections()
    # reset_queries() clears the per-connection query log, which accumulates for
    # the worker's lifetime when DEBUG is True (no request cycle to clear it).
    reset_queries()


# thread_sensitive=True (asgiref default, load-bearing here) runs cleanup on the
# same thread that owns the connections — where the async ORM routes queries.
_acleanup_before = sync_to_async(close_old_connections, thread_sensitive=True)
_acleanup_after = sync_to_async(_cleanup_after, thread_sensitive=True)


def task(original_func=None, **task_kwargs):
    """Drop-in replacement for ``@app.task`` that survives dead DB connections.

    The worker is long-lived with no HTTP request cycle, so Django's
    request_started/finished hooks never run and a connection that dies under it
    (RDS restart/upgrade, idle TCP timeout) is reused — closed — forever, failing
    every later ORM call with ``the connection is closed``, including the janitor
    task meant to rescue jobs stranded by exactly this (June 2026 incident: ~22h
    of failed background jobs after an RDS upgrade). So we mirror Django's
    per-request connection management around each task body.

    Task-middleware pattern from the procrastinate docs (howto/advanced/middleware)
    so individual tasks can't forget the hygiene; ``tests/test_worker_db_resilience.py``
    enforces every task in ``apps.workspaces.tasks`` is registered through it.

    TEMPORARY workaround for procrastinate-org/procrastinate#1134; upstream PR #1555
    adds this natively in the Django contrib. Strip once we upgrade past its release
    — see https://github.com/dimagi-rad/scout/issues/225.
    """

    def wrap(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            await _acleanup_before()
            try:
                return await func(*args, **kwargs)
            finally:
                await _acleanup_after()

        wrapper._ensures_fresh_db_connections = True
        return app.task(**task_kwargs)(wrapper)

    if original_func:
        return wrap(original_func)
    return wrap
