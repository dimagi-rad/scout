"""Worker DB-connection resilience.

The procrastinate worker is a long-lived process with no HTTP request cycle,
so Django's request_started/request_finished hooks never run and a DB
connection that dies (RDS restart/upgrade, idle TCP timeout) is reused —
closed — forever. In the June 2026 prod incident every background task failed
for ~22h with ``psycopg.OperationalError: the connection is closed``,
including the janitor that should have rescued the stuck jobs.

These tests pin the fix: tasks are registered through the custom ``task``
decorator (config/procrastinate.py), which closes stale/dead connections —
so they re-open on next use — before the task body runs.
"""

import inspect

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import OperationalError, connections

import apps.workspaces.tasks as workspace_tasks
from config.procrastinate import app, task

User = get_user_model()


@task(name="tests.fake_task_db_resilience")
async def fake_task() -> int:
    return await User.objects.acount()


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
    """Control: documents the failure mode the task decorator exists to fix."""
    await sync_to_async(_kill_default_connection)()
    with pytest.raises(OperationalError):
        await User.objects.acount()
    # Clean up for subsequent tests sharing this thread's connection. The
    # wrapper lookup must happen inside the executor thread that owns it,
    # not in the event-loop thread.
    await sync_to_async(lambda: connections["default"].close())()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_task_recovers_from_dead_connection():
    """A task registered through the custom decorator succeeds even when the
    worker's connection died since the previous job. ``fake_task.func`` is
    the wrapped body exactly as the procrastinate worker would invoke it."""
    await sync_to_async(_kill_default_connection)()
    try:
        assert await fake_task.func() == 0
    finally:
        # Close the reconnected session so test-database teardown can drop
        # the DB without "is being accessed by other users" warnings.
        await sync_to_async(lambda: connections["default"].close())()


def test_all_workspace_tasks_are_wrapped():
    """Every procrastinate task must be registered via the custom ``task``
    decorator; a task registered with bare ``@app.task`` reintroduces the
    permanent-dead-connection bug."""
    workspace_task_funcs = [
        t
        for t in app.tasks.values()
        if inspect.unwrap(t.func).__module__ == workspace_tasks.__name__
    ]
    assert workspace_task_funcs, "expected procrastinate tasks registered from tasks.py"
    unwrapped = [
        t.name
        for t in workspace_task_funcs
        if not getattr(t.func, "_ensures_fresh_db_connections", False)
    ]
    assert not unwrapped, f"tasks not registered via config.procrastinate.task: {unwrapped}"
