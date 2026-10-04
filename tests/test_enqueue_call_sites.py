"""The queue rows each production enqueue site writes, read back from procrastinate_jobs.

Queued rows are an external contract: workers match them by task name and arguments,
and locks dedupe them. These pins are on the persisted row, not on how a call site
reaches the queue, so moving dispatch behind another API must leave them unchanged.
"""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from django.db import connection
from django.test import AsyncClient
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from rest_framework.test import APIClient

from apps.chat.models import Thread, ThreadJob
from apps.chat.services import continuation
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services import thread_job_dispatch
from apps.workspaces.services.load_generations import (
    INTENT_RECONCILE_MISSING,
    capture_load_intent,
)
from apps.workspaces.services.reconciliation import reconcile_stale_thread_job
from apps.workspaces.services.workspace_service import (
    _defer_unserved_load,
    add_workspace_tenant,
    remove_workspace_tenant,
)
from tests.tenant_access import acovered_source

TASKS = "apps.workspaces.tasks."


def _row(job: ProcrastinateJob) -> dict:
    return {
        "task_name": job.task_name,
        "queue_name": job.queue_name,
        "priority": job.priority,
        "lock": job.lock,
        "queueing_lock": job.queueing_lock,
        "args": job.args,
    }


def _queued(**args) -> list[ProcrastinateJob]:
    return list(ProcrastinateJob.objects.filter(args__contains=args).order_by("id"))


async def _aqueued(**args) -> list[ProcrastinateJob]:
    return [
        job async for job in ProcrastinateJob.objects.filter(args__contains=args).order_by("id")
    ]


def _expected(task: str, args: dict, *, queueing_lock: str | None = None) -> dict:
    return {
        "task_name": TASKS + task,
        "queue_name": "default",
        "priority": 0,
        "lock": None,
        "queueing_lock": queueing_lock,
        "args": args,
    }


def _assert_scheduled_in(job: ProcrastinateJob, seconds: int, before, after) -> None:
    assert before + timedelta(seconds=seconds) <= job.scheduled_at
    assert job.scheduled_at <= after + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _drop_queued_rows(django_db_blocker):
    # procrastinate_jobs is unmanaged: rows committed by transactional tests outlive them.
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM procrastinate_jobs")
        (start,) = cursor.fetchone()
    yield
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id > %s", [start])


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(
        provider="commcare", external_id="enqueue-domain-2", canonical_name="Enqueue Domain 2"
    )


@pytest.fixture
def tenant3(db):
    return Tenant.objects.create(
        provider="commcare", external_id="enqueue-domain-3", canonical_name="Enqueue Domain 3"
    )


@pytest.mark.django_db
def test_unserved_load_queues_a_quiet_reconcile_load(workspace, tenant, user):
    intent = capture_load_intent([tenant.id], INTENT_RECONCILE_MISSING)

    _defer_unserved_load(workspace.id, [tenant.id], user.id)

    assert [_row(job) for job in _queued(workspace_id=str(workspace.id))] == [
        _expected(
            "materialize_workspace",
            {
                "workspace_id": str(workspace.id),
                "user_id": str(user.id),
                "load_intent": intent,
                "only_unserved": True,
                "notify_thread": False,
            },
        )
    ]
    assert _queued(workspace_id=str(workspace.id))[0].scheduled_at is None


@pytest.mark.django_db
def test_adding_a_serving_source_queues_a_view_rebuild(workspace, tenant, tenant2):
    TenantSchema.objects.create(tenant=tenant2, schema_name="t_enqueue_2", state=SchemaState.ACTIVE)

    add_workspace_tenant(workspace, tenant2)

    assert [_row(job) for job in _queued(workspace_id=str(workspace.id))] == [
        _expected("rebuild_workspace_view_schema", {"workspace_id": str(workspace.id)})
    ]


@pytest.mark.django_db
def test_removing_down_to_one_source_queues_view_teardown(workspace, tenant2):
    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_enqueue", state=SchemaState.ACTIVE
    )

    remove_workspace_tenant(workspace, wt)

    assert [_row(job) for job in _queued(view_schema_id=str(vs.id))] == [
        _expected("teardown_view_schema_task", {"view_schema_id": str(vs.id)})
    ]


@pytest.mark.django_db
def test_removing_one_of_several_sources_queues_a_view_rebuild(workspace, tenant, tenant2, tenant3):
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    TenantSchema.objects.create(tenant=tenant, schema_name="t_enqueue_1", state=SchemaState.ACTIVE)
    wt3 = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant3)
    WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_enqueue", state=SchemaState.ACTIVE
    )

    remove_workspace_tenant(workspace, wt3)

    assert [_row(job) for job in _queued(workspace_id=str(workspace.id))] == [
        _expected("rebuild_workspace_view_schema", {"workspace_id": str(workspace.id)})
    ]


@pytest.mark.django_db
def test_refresh_settles_a_pruned_candidate_and_queues_the_refresh(workspace, tenant, user):
    membership, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    stale = TenantSchema.objects.create(
        tenant=tenant,
        schema_name="t_enqueue_stale",
        state=SchemaState.PROVISIONING,
        refresh_job_id=987654321,
        refresh_workspace_id=workspace.id,
        refresh_actor_user_id=user.id,
        refresh_membership_id=membership.id,
    )
    TenantSchema.objects.filter(id=stale.id).update(created_at=timezone.now() - timedelta(days=30))
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert resp.status_code == 202, resp.data
    assert [_row(job) for job in _queued(schema_id=str(stale.id))] == [
        _expected("drop_failed_refresh_schema", {"schema_id": str(stale.id)})
    ]
    new_schema_id = resp.data["schema_id"]
    assert [_row(job) for job in _queued(schema_id=new_schema_id)] == [
        _expected(
            "refresh_tenant_schema",
            {
                "schema_id": new_schema_id,
                "membership_id": str(membership.id),
                "actor_user_id": str(user.id),
                "workspace_id": str(workspace.id),
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_load_dispatch_queues_the_plain_load(workspace, user):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    intent = {"tenant-a": 3}

    tj = await thread_job_dispatch.adispatch_thread_materialization(
        thread_id=thread.id,
        tool_call_id="tc-enqueue",
        workspace_id=str(workspace.id),
        user_id=str(user.id),
        load_intent=intent,
    )

    jobs = await _aqueued(workspace_id=str(workspace.id))
    assert [_row(job) for job in jobs] == [
        _expected(
            "materialize_workspace",
            {"workspace_id": str(workspace.id), "user_id": str(user.id), "load_intent": intent},
        )
    ]
    assert jobs[0].scheduled_at is None
    assert tj.procrastinate_job_id == jobs[0].id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_load_dispatch_queues_a_locked_delayed_unserved_load(workspace, user):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    before = timezone.now()

    await thread_job_dispatch.adispatch_thread_materialization(
        thread_id=thread.id,
        tool_call_id="tc-enqueue",
        workspace_id=str(workspace.id),
        user_id="",
        load_intent=None,
        queueing_lock=f"enqueue-lock-{workspace.id}",
        start_in_seconds=45,
        only_unserved=True,
    )

    after = timezone.now()
    jobs = await _aqueued(workspace_id=str(workspace.id))
    assert [_row(job) for job in jobs] == [
        _expected(
            "materialize_workspace",
            {
                "workspace_id": str(workspace.id),
                "user_id": "",
                "load_intent": None,
                "only_unserved": True,
            },
            queueing_lock=f"enqueue-lock-{workspace.id}",
        )
    ]
    _assert_scheduled_in(jobs[0], 45, before, after)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_semantic_rebuild_queues_a_workspace_recovery(workspace, user):
    recovery = await thread_job_dispatch._adispatch_semantic_rebuild(
        workspace_id=workspace.id, user_id=user.id
    )

    jobs = await _aqueued(recovery_id=str(recovery.id))
    assert [_row(job) for job in jobs] == [
        _expected("recover_workspace_data", {"recovery_id": str(recovery.id)})
    ]
    assert jobs[0].scheduled_at is None
    assert recovery.procrastinate_job_id == jobs[0].id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_retry_without_a_thread_queues_a_quiet_load():
    user = await User.objects.acreate_user(email="enqueue-retry@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await acovered_source(ws, user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    client = AsyncClient()
    await client.alogin(email="enqueue-retry@b.c", password="x")

    resp = await client.post(
        f"/api/workspaces/{ws.id}/materialize/retry/",
        data=json.dumps({}),
        content_type="application/json",
    )

    assert resp.status_code == 200, resp.content
    jobs = await _aqueued(workspace_id=str(ws.id))
    assert len(jobs) == 1
    row = _row(jobs[0])
    intent = row["args"]["load_intent"]
    assert row == _expected(
        "materialize_workspace",
        {
            "workspace_id": str(ws.id),
            "user_id": str(user.id),
            "load_intent": intent,
            "notify_thread": False,
        },
    )
    assert isinstance(intent, dict)
    assert jobs[0].scheduled_at is None
    assert resp.json() == {"status": "started", "procrastinate_job_id": jobs[0].id}


async def _thread_job(workspace, user) -> ThreadJob:
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    return await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=424242,
        tool_call_id="tc-enqueue",
        state=ThreadJob.State.PENDING,
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_busy_resume_requeues_itself_with_backoff_and_lock(workspace, user):
    tj = await _thread_job(workspace, user)
    before = timezone.now()

    result = await continuation._defer_resume_while_thread_busy(tj, 1)

    after = timezone.now()
    delay = result["retry_in_seconds"]
    assert delay == min(
        continuation.RESUME_BUSY_RETRY_BASE_SECONDS * 2,
        continuation.RESUME_BUSY_RETRY_MAX_SECONDS,
    )
    jobs = await _aqueued(thread_job_id=str(tj.id))
    assert [_row(job) for job in jobs] == [
        _expected(
            "resume_thread_after_materialization",
            {"thread_job_id": str(tj.id), "busy_attempt": 2},
            queueing_lock=f"resume-thread-busy-{tj.id}",
        )
    ]
    _assert_scheduled_in(jobs[0], delay, before, after)

    again = await continuation._defer_resume_while_thread_busy(tj, 1)

    assert again == {"status": "thread_busy_already_queued"}
    assert len(await _aqueued(thread_job_id=str(tj.id))) == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_pending_flush_queues_one_delayed_locked_flush(workspace):
    before = timezone.now()

    await continuation.defer_pending_flush(workspace.id, 7)
    await continuation.defer_pending_flush(workspace.id, 7)

    after = timezone.now()
    jobs = await _aqueued(workspace_id=str(workspace.id))
    assert [_row(job) for job in jobs] == [
        _expected(
            "flush_pending_requests",
            {"workspace_id": str(workspace.id)},
            queueing_lock=f"pending-flush:{workspace.id}",
        )
    ]
    _assert_scheduled_in(jobs[0], 7, before, after)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_queues_a_plain_resume_for_an_unclaimed_success(workspace, user):
    tj = await _thread_job(workspace, user)
    await ThreadJob.objects.filter(id=tj.id).aupdate(created_at=timezone.now() - timedelta(hours=2))
    await tj.arefresh_from_db()

    with patch(
        "apps.workspaces.services.reconciliation._procrastinate_job_status",
        new=AsyncMock(return_value="succeeded"),
    ):
        await reconcile_stale_thread_job(tj)

    jobs = await _aqueued(thread_job_id=str(tj.id))
    assert [_row(job) for job in jobs] == [
        _expected("resume_thread_after_materialization", {"thread_job_id": str(tj.id)})
    ]
    assert jobs[0].scheduled_at is None
