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
    # Don't leak the connection past the unit of work...
    close_old_connections()
    # ...and clear Django's per-connection query log, which is only populated
    # when settings.DEBUG is True and otherwise accumulates for the lifetime
    # of the worker. Mirrors what Django does at request_started; a no-op
    # when DEBUG is False.
    reset_queries()


# Cleanup must run on the thread that owns the connections: asgiref's
# thread-sensitive executor, the same thread the async ORM routes queries
# through. thread_sensitive=True is the asgiref default — spelled out because
# it is load-bearing here.
_acleanup_before = sync_to_async(close_old_connections, thread_sensitive=True)
_acleanup_after = sync_to_async(_cleanup_after, thread_sensitive=True)


def task(original_func=None, **task_kwargs):
    """Drop-in replacement for ``@app.task`` that survives dead DB connections.

    The worker is a long-lived process with no HTTP request cycle, so Django's
    request_started/request_finished hooks never run and a connection that dies
    underneath it (RDS restart or upgrade, idle TCP timeout) is reused — closed
    — forever: every subsequent ORM call fails instantly with
    ``psycopg.OperationalError: the connection is closed``, including the
    janitor task that is supposed to rescue jobs stranded by exactly this
    (June 2026 incident: ~22h of failed background jobs after an RDS upgrade).

    Mirrors Django's per-request connection management around each task —
    ``close_old_connections()`` before the body (so a connection that died
    between jobs is replaced instead of reused) and after it (so nothing leaks
    past the unit of work), plus ``reset_queries()`` after.

    This is the task-middleware pattern from the procrastinate docs
    (howto/advanced/middleware): one decorator wraps the body and delegates
    registration to ``@app.task``, so individual tasks can't forget the
    connection hygiene. ``tests/test_worker_db_resilience.py`` enforces that
    every task in ``apps.workspaces.tasks`` is registered through it.

    TEMPORARY: this is a workaround for procrastinate-org/procrastinate#1134;
    upstream PR #1555 adds the same cleanup natively in the Django contrib.
    Once that merges and we upgrade past the release containing it, strip this
    wrapper — see https://github.com/dimagi-rad/scout/issues/225.
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
