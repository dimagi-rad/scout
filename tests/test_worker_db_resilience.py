"""Worker DB-connection resilience.

The procrastinate worker is a long-lived process with no HTTP request cycle,
so Django's request_started/request_finished hooks never run and a DB
connection that dies (RDS restart/upgrade, idle TCP timeout) is reused —
closed — forever. In the June 2026 prod incident every background task failed
for ~22h with ``psycopg.OperationalError: the connection is closed``,
including the janitor that should have rescued the stuck jobs.

These tests pin the fix: every procrastinate task is wrapped so stale/dead
connections are closed (and therefore re-opened on next use) before the task
body runs.
"""

import inspect

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import OperationalError, connections

import apps.workspaces.tasks as workspace_tasks
from config.procrastinate import app, ensure_fresh_db_connections

User = get_user_model()


def _kill_default_connection():
    """Close the underlying psycopg connection behind Django's back.

    Django still holds the (now closed) connection object, which is exactly
    the state the worker was stuck in: the next cursor raises
    ``OperationalError: the connection is closed``.
    """
    conn = connections["default"]
    conn.ensure_connection()
    conn.connection.close()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_orm_call_fails_on_dead_connection_without_recovery():
    """Control: documents the failure mode the wrapper exists to fix."""
    await sync_to_async(_kill_default_connection)()
    with pytest.raises(OperationalError):
        await User.objects.acount()
    # Clean up for subsequent tests sharing this thread's connection. The
    # wrapper lookup must happen inside the executor thread that owns it,
    # not in the event-loop thread.
    await sync_to_async(lambda: connections["default"].close())()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_wrapped_task_recovers_from_dead_connection():
    """A task body wrapped with ensure_fresh_db_connections succeeds even when
    the worker's connection died since the previous job."""

    @ensure_fresh_db_connections
    async def fake_task() -> int:
        return await User.objects.acount()

    await sync_to_async(_kill_default_connection)()
    try:
        assert await fake_task() == 0
    finally:
        # Close the reconnected session so test-database teardown can drop
        # the DB without "is being accessed by other users" warnings.
        await sync_to_async(lambda: connections["default"].close())()


def test_all_workspace_tasks_are_wrapped():
    """Every procrastinate task must opt into connection recovery; a new task
    that skips the wrapper reintroduces the permanent-dead-connection bug."""
    workspace_task_funcs = [
        task
        for task in app.tasks.values()
        if inspect.unwrap(task.func).__module__ == workspace_tasks.__name__
    ]
    assert workspace_task_funcs, "expected procrastinate tasks registered from tasks.py"
    unwrapped = [
        task.name
        for task in workspace_task_funcs
        if not getattr(task.func, "_ensures_fresh_db_connections", False)
    ]
    assert not unwrapped, f"tasks missing ensure_fresh_db_connections: {unwrapped}"
