"""The stale-materialization sweep fails view schemas whose build will never finish (#366).

Real control PostgreSQL: real procrastinate job and worker rows, and a real
workspace advisory lock W for the live-builder case.
"""

import json
import logging
import uuid
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.utils import timezone

from apps.workspaces.models import SchemaState, WorkspaceViewSchema
from apps.workspaces.services.data_operation import sync_workspace_data_lock
from apps.workspaces.tasks import (
    MATERIALIZATION_STALLED_HEARTBEAT_SECONDS,
    ORPHANED_VIEW_BUILD_ERROR,
    materialize_workspace,
    rebuild_workspace_view_schema,
    reconcile_stale_materialization_runs,
)

pytestmark = pytest.mark.django_db(transaction=True)

STALLED = timedelta(seconds=MATERIALIZATION_STALLED_HEARTBEAT_SECONDS) + timedelta(minutes=1)


@pytest.fixture
def queue():
    workers, jobs = [], []

    def job(task_name, workspace_id, *, status, heartbeat_age=None):
        with connection.cursor() as cursor:
            worker_id = None
            if heartbeat_age is not None:
                cursor.execute(
                    "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (%s) RETURNING id",
                    [timezone.now() - heartbeat_age],
                )
                worker_id = cursor.fetchone()[0]
                workers.append(worker_id)
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args, worker_id) "
                "VALUES (%s, %s, %s::procrastinate_job_status, %s::jsonb, %s) RETURNING id",
                [
                    "default",
                    task_name,
                    status,
                    json.dumps({"workspace_id": str(workspace_id)}),
                    worker_id,
                ],
            )
            jobs.append(cursor.fetchone()[0])

    yield job

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [jobs])
        cursor.execute("DELETE FROM procrastinate_workers WHERE id = ANY(%s)", [workers])


def _view_schema(workspace, state=SchemaState.PROVISIONING):
    return WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name=f"ws_{uuid.uuid4().hex[:16]}", state=state
    )


def _sweep():
    return async_to_sync(reconcile_stale_materialization_runs.func)()


@pytest.mark.parametrize(
    ("status", "heartbeat_age"),
    [
        ("doing", None),
        ("doing", STALLED),
        ("failed", None),
        (None, None),
    ],
    ids=["worker-gone", "heartbeat-stale", "job-ended", "no-job"],
)
def test_a_view_schema_no_live_job_will_build_is_failed_and_reported(
    workspace, queue, caplog, status, heartbeat_age
):
    vs = _view_schema(workspace)
    if status is not None:
        queue(
            rebuild_workspace_view_schema.name,
            workspace.id,
            status=status,
            heartbeat_age=heartbeat_age,
        )

    with caplog.at_level(logging.ERROR, logger="apps.workspaces.tasks"):
        result = _sweep()

    vs.refresh_from_db()
    assert vs.state == SchemaState.FAILED
    assert vs.last_error == ORPHANED_VIEW_BUILD_ERROR
    assert result["view_schemas_settled"] == 1
    assert [r.levelno for r in caplog.records if str(vs.id) in r.getMessage()] == [logging.ERROR]


@pytest.mark.parametrize(
    "task_name", [rebuild_workspace_view_schema.name, materialize_workspace.name]
)
@pytest.mark.parametrize(
    ("status", "heartbeat_age"),
    [("todo", None), ("doing", timedelta(0))],
    ids=["queued", "running"],
)
def test_a_view_schema_a_live_job_will_build_is_left_alone(
    workspace, queue, task_name, status, heartbeat_age
):
    vs = _view_schema(workspace)
    queue(task_name, workspace.id, status=status, heartbeat_age=heartbeat_age)

    result = _sweep()

    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING
    assert result["view_schemas_settled"] == 0


def test_another_workspaces_live_job_does_not_keep_this_view_schema(workspace, queue):
    vs = _view_schema(workspace)
    queue(rebuild_workspace_view_schema.name, uuid.uuid4(), status="todo")

    _sweep()

    vs.refresh_from_db()
    assert vs.state == SchemaState.FAILED


def test_a_workspace_whose_lock_is_held_has_a_live_builder_and_is_skipped(workspace):
    vs = _view_schema(workspace)

    # A job-less build (the agent's blocking load) is visible only through W.
    with sync_workspace_data_lock(workspace.id):
        result = _sweep()

    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING
    assert result["view_schemas_settled"] == 0


@pytest.mark.parametrize("state", [SchemaState.ACTIVE, SchemaState.FAILED, SchemaState.TEARDOWN])
def test_view_schemas_outside_provisioning_are_untouched(workspace, state):
    vs = _view_schema(workspace, state=state)

    _sweep()

    vs.refresh_from_db()
    assert vs.state == state
    assert vs.last_error == ""
