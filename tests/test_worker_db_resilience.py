"""Worker DB-connection resilience.

The procrastinate worker is a long-lived process with no HTTP request cycle,
so Django's request_started/request_finished hooks never run and a DB
connection that dies (RDS restart/upgrade, idle TCP timeout) is reused —
closed — forever. In the June 2026 prod incident every background task failed
for ~22h with ``psycopg.OperationalError: the connection is closed``,
including the janitor that should have rescued the stuck jobs.

Since procrastinate 3.9 the Django contrib's ``DjangoApp`` closes stale
connections before and after every task via worker-wide task middleware
(procrastinate#1577), which replaced our custom ``task`` decorator (#225).
The middleware runs inside the worker, not in ``task.func``, so these tests
drive jobs through a real worker on an in-memory connector.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import OperationalError, connections
from procrastinate.contrib.django.db_cleanup import (
    close_db_connections,
    close_db_connections_async,
)
from procrastinate.testing import InMemoryConnector

from config.procrastinate import app

User = get_user_model()

QUEUE = "tests_db_resilience"


@app.task(name="tests.fake_task_db_resilience", queue=QUEUE)
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


async def _run_one_job() -> str:
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        async with app.open_async():
            job_id = await fake_task.defer_async()
            await app.run_worker_async(
                queues=[QUEUE],
                wait=False,
                install_signal_handlers=False,
                listen_notify=False,
            )
    return connector.jobs[job_id]["status"]


def test_app_manages_db_connections_around_tasks():
    """The app every task registers on must be the contrib's ``DjangoApp``, with
    its cleanup middleware configured worker-wide; a bare ``procrastinate.App``
    (or a ``task_middleware`` override in worker options) reintroduces the
    permanent-dead-connection bug."""
    middleware = app.worker_defaults.get("task_middleware", [])
    assert close_db_connections_async in middleware
    assert close_db_connections in middleware


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_orm_call_fails_on_dead_connection_without_recovery():
    """Control: documents the failure mode the worker middleware exists to fix."""
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
    """A job succeeds even when the worker's connection died since the
    previous job."""
    await sync_to_async(_kill_default_connection)()
    assert await _run_one_job() == "succeeded"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_task_closes_connections_after_body():
    """The worker also cleans up after the task body (mirroring Django's
    request_finished) so it doesn't hold a connection open between jobs —
    with the default CONN_MAX_AGE=0 it must be closed."""
    assert await _run_one_job() == "succeeded"

    def _connection_is_closed():
        return connections["default"].connection is None

    assert await sync_to_async(_connection_is_closed)()
