"""One waiting rebuild per workspace: later requests join it (#918).

Adding several sources used to queue a view rebuild (and its Cube validation)
per source. A rebuild reads the workspace when it runs, so a waiting one covers
every later request; one that is already running does not, so those still queue.
"""

import uuid

import pytest
from django.db import connection, transaction
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.workspaces import task_dispatch
from apps.workspaces.task_dispatch import (
    REBUILD_WORKSPACE_SEMANTIC_MODEL,
    REBUILD_WORKSPACE_VIEW_SCHEMA,
    adefer_rebuild_workspace_semantic_model,
    adefer_rebuild_workspace_view_schema,
    defer_rebuild_workspace_view_schema,
)


@pytest.fixture(autouse=True)
def _drop_queued_rows(django_db_blocker):
    # procrastinate_jobs is unmanaged: rows committed by transactional tests outlive them.
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM procrastinate_jobs")
        (start,) = cursor.fetchone()
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id > %s", [start])


def _jobs(task_name: str, workspace_id: str) -> list[ProcrastinateJob]:
    return list(
        ProcrastinateJob.objects.filter(
            task_name=task_name, args__workspace_id=workspace_id
        ).order_by("id")
    )


async def _ajobs(task_name: str, workspace_id: str) -> list[ProcrastinateJob]:
    return [
        job
        async for job in ProcrastinateJob.objects.filter(
            task_name=task_name, args__workspace_id=workspace_id
        ).order_by("id")
    ]


def _start(job_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [job_id])


@pytest.mark.django_db(transaction=True)
def test_a_waiting_view_rebuild_absorbs_later_requests():
    ws = str(uuid.uuid4())
    first = defer_rebuild_workspace_view_schema(workspace_id=ws)

    assert defer_rebuild_workspace_view_schema(workspace_id=ws) is None
    assert defer_rebuild_workspace_view_schema(workspace_id=ws) is None

    jobs = _jobs(REBUILD_WORKSPACE_VIEW_SCHEMA, ws)
    assert [job.id for job in jobs] == [first]
    assert jobs[0].queueing_lock == f"view-rebuild:{ws}"


@pytest.mark.django_db(transaction=True)
def test_each_workspace_gets_its_own_rebuild():
    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    assert defer_rebuild_workspace_view_schema(workspace_id=one) is not None
    assert defer_rebuild_workspace_view_schema(workspace_id=two) is not None


@pytest.mark.django_db(transaction=True)
def test_a_request_during_a_running_rebuild_still_queues_one():
    ws = str(uuid.uuid4())
    running = defer_rebuild_workspace_view_schema(workspace_id=ws)
    _start(running)

    queued = defer_rebuild_workspace_view_schema(workspace_id=ws)

    assert queued is not None
    assert [(job.id, job.status) for job in _jobs(REBUILD_WORKSPACE_VIEW_SCHEMA, ws)] == [
        (running, "doing"),
        (queued, "todo"),
    ]


@pytest.mark.django_db(transaction=True)
def test_a_duplicate_inside_a_transaction_leaves_it_usable():
    ws = str(uuid.uuid4())
    defer_rebuild_workspace_view_schema(workspace_id=ws)

    with transaction.atomic():
        assert defer_rebuild_workspace_view_schema(workspace_id=ws) is None
        # Without the savepoint the unique violation would abort the transaction.
        assert len(_jobs(REBUILD_WORKSPACE_VIEW_SCHEMA, ws)) == 1


@pytest.mark.django_db(transaction=True)
def test_a_rebuild_that_starts_before_the_commit_is_followed_by_another():
    """The waiting rebuild can start before the caller commits and miss its writes."""
    ws = str(uuid.uuid4())
    waiting = defer_rebuild_workspace_view_schema(workspace_id=ws)

    with transaction.atomic():
        assert defer_rebuild_workspace_view_schema(workspace_id=ws) is None
        assert task_dispatch.connection.in_atomic_block
        # A worker picks the waiting job up on its own connection before we commit.
        _start(waiting)

    statuses = [job.status for job in _jobs(REBUILD_WORKSPACE_VIEW_SCHEMA, ws)]
    assert statuses == ["doing", "todo"]


@pytest.mark.django_db(transaction=True)
def test_a_rebuild_still_waiting_at_commit_covers_the_transaction():
    ws = str(uuid.uuid4())
    defer_rebuild_workspace_view_schema(workspace_id=ws)

    with transaction.atomic():
        assert defer_rebuild_workspace_view_schema(workspace_id=ws) is None

    assert [job.status for job in _jobs(REBUILD_WORKSPACE_VIEW_SCHEMA, ws)] == ["todo"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_view_rebuild_requests_are_deduplicated():
    ws = str(uuid.uuid4())
    first = await adefer_rebuild_workspace_view_schema(workspace_id=ws)

    assert await adefer_rebuild_workspace_view_schema(workspace_id=ws) is None
    assert [job.id for job in await _ajobs(REBUILD_WORKSPACE_VIEW_SCHEMA, ws)] == [first]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_semantic_rebuilds_are_deduplicated_separately_from_view_rebuilds():
    ws = str(uuid.uuid4())
    await adefer_rebuild_workspace_view_schema(workspace_id=ws)
    first = await adefer_rebuild_workspace_semantic_model(workspace_id=ws)

    assert first is not None
    assert await adefer_rebuild_workspace_semantic_model(workspace_id=ws) is None
    [job] = await _ajobs(REBUILD_WORKSPACE_SEMANTIC_MODEL, ws)
    assert job.queueing_lock == f"semantic-rebuild:{ws}"
