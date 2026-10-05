import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import AsyncClient
from django.utils import timezone

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import MaterializationRun, TenantSchema, Workspace, WorkspaceLoadTiming
from apps.workspaces.services.load_phases import LoadPhase
from apps.workspaces.services.load_time_estimates import (
    afinish_load_timing,
    aload_time_estimate,
    aload_time_estimates,
    astart_load_timing,
    atransition_load_phase,
)
from apps.workspaces.services.materialize import _apublish_phase, materialize_workspace

User = get_user_model()


async def make_workspace():
    user = await User.objects.acreate_user(email="estimates@example.test")
    return await Workspace.objects.acreate(name="Estimate", created_by=user)


async def sample(ws, job, seconds, success=True, phases=None):
    start = timezone.now() - timedelta(seconds=seconds + job)
    return await WorkspaceLoadTiming.objects.acreate(
        workspace=ws,
        job_id=job,
        started_at=start,
        completed_at=start + timedelta(seconds=seconds),
        succeeded=success,
        phase_seconds=phases or {},
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_no_history_has_no_estimate():
    ws = await make_workspace()
    assert await aload_time_estimate(ws.id, 100) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_one_successful_run_and_current_elapsed():
    ws = await make_workspace()
    await sample(ws, 1, 240, phases={"building_model": 30})
    await astart_load_timing(ws.id, 100)
    now = timezone.now()
    await WorkspaceLoadTiming.objects.filter(job_id=100).aupdate(
        started_at=now - timedelta(seconds=60),
    )
    estimate = await aload_time_estimate(ws.id, 100, now=now)
    assert estimate["usual_seconds"] == 240
    assert estimate["elapsed_seconds"] == 60
    assert estimate["phase_seconds"] == {"building_model": 30}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_median_excludes_failures_and_outliers_do_not_dominate():
    ws = await make_workspace()
    for job, seconds in enumerate([100, 110, 120, 130, 9000], 1):
        await sample(ws, job, seconds)
    await sample(ws, 6, 1, success=False)
    await astart_load_timing(ws.id, 100)
    estimate = await aload_time_estimate(ws.id, 100)
    assert estimate["usual_seconds"] == 120
    assert estimate["sample_count"] == 5


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_samples_are_workspace_scoped_and_bounded():
    ws = await make_workspace()
    other = await Workspace.objects.acreate(name="Other", created_by=ws.created_by)
    await sample(other, 50, 9999)
    for job in range(1, 15):
        await sample(ws, job, 100 + job)
    await astart_load_timing(ws.id, 100)
    estimate = await aload_time_estimate(ws.id, 100)
    assert estimate["sample_count"] == 10
    assert estimate["usual_seconds"] == 105.5


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_repeated_phases_accumulate_and_finish_closes_last_phase():
    ws = await make_workspace()
    await astart_load_timing(ws.id, 100)
    start = timezone.now()
    await WorkspaceLoadTiming.objects.filter(job_id=100).aupdate(
        started_at=start,
        phase_started_at=start,
    )
    await atransition_load_phase(100, "building_tables", now=start + timedelta(seconds=10))
    await atransition_load_phase(100, "checking_quality", now=start + timedelta(seconds=30))
    await atransition_load_phase(100, "building_tables", now=start + timedelta(seconds=35))
    await afinish_load_timing(100, True, now=start + timedelta(seconds=45))
    timing = await WorkspaceLoadTiming.objects.aget(job_id=100)
    assert timing.succeeded
    assert timing.phase_seconds == {"loading": 10, "building_tables": 30, "checking_quality": 5}
    assert timing.completed_at == start + timedelta(seconds=45)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_failed_history_alone_has_no_estimate():
    ws = await make_workspace()
    await sample(ws, 1, 10, success=False)
    assert await aload_time_estimate(ws.id, 100) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_legacy_completed_jobs_are_not_treated_as_successful_history():
    """Old completion can hide a failed Cube rebuild; never infer a timing sample."""
    ws = await make_workspace()
    start = timezone.now() - timedelta(minutes=10)
    thread = await Thread.objects.acreate(workspace=ws, user_id=ws.created_by_id)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=1,
        state="completed",
        started_at=start + timedelta(seconds=300),
    )
    tenant = await Tenant.objects.acreate(external_id="estimate", provider="commcare")
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="estimate")
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="test",
        state="completed",
        procrastinate_job_id=1,
        completed_at=start + timedelta(seconds=200),
    )
    await MaterializationRun.objects.filter(pk=run.pk).aupdate(started_at=start)
    assert await aload_time_estimate(ws.id, 100) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_only_unserved_history_is_separate():
    ws = await make_workspace()
    await sample(ws, 1, 240)
    await astart_load_timing(ws.id, 100, only_unserved=True)
    assert await aload_time_estimate(ws.id, 100) is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_job_api_exposes_optional_estimate(workspace, write_user):
    await sample(workspace, 1, 240)
    thread = await Thread.objects.acreate(workspace=workspace, user=write_user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=100,
        state="pending",
    )
    await astart_load_timing(workspace.id, 100)
    client = AsyncClient()
    await client.aforce_login(write_user)
    response = await client.get(f"/api/workspaces/{workspace.id}/jobs/active/")
    assert response.status_code == 200
    assert response.json()["jobs"][0]["time_estimate"]["usual_seconds"] == 240


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "cube_ok,run_state,success",
    [
        (True, "completed", True),
        (False, "completed", False),
        (True, "partial", False),
    ],
)
async def test_worker_records_only_full_success(workspace, tenant, cube_ok, run_state, success):
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="worker_timing")
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="test",
        state=run_state,
        procrastinate_job_id=100,
        completed_at=timezone.now(),
    )
    core_result = {
        "all_succeeded": True,
        "tenants": [],
        "cube_schema": {"ok": cube_ok},
        "view_schema": None,
    }
    context = SimpleNamespace(job=SimpleNamespace(id=100))
    with (
        patch(
            "apps.workspaces.services.materialize.materialize_workspace_core",
            AsyncMock(return_value=core_result),
        ),
        patch("apps.workspaces.services.materialize._defer_resume_for_job", AsyncMock()) as resume,
        patch("apps.workspaces.services.materialize._defer_pending_flush", AsyncMock()),
    ):
        await materialize_workspace(context, str(workspace.id))
    timing = await WorkspaceLoadTiming.objects.aget(job_id=100)
    assert timing.completed_at is not None
    assert timing.succeeded is success
    resume.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_phase_publication_updates_timing_and_workspace_load_payload(workspace, tenant):
    await sample(workspace, 1, 240)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="phase_timing",
        load_workspace_id=workspace.id,
        load_job_id=100,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="test",
        state="loading",
        procrastinate_job_id=100,
    )
    await astart_load_timing(workspace.id, 100)
    await _apublish_phase(100, LoadPhase.BUILDING_MODEL, "Preparing")
    timing = await WorkspaceLoadTiming.objects.aget(job_id=100)
    assert timing.phase == "building_model"
    client = AsyncClient()
    await client.aforce_login(workspace.created_by)
    response = await client.get(f"/api/workspaces/{workspace.id}/jobs/active/")
    assert response.status_code == 200
    assert response.json()["workspace_loads"][0]["time_estimate"]["usual_seconds"] == 240


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_invalid_durations_do_not_consume_history_slots():
    ws = await make_workspace()
    await sample(ws, 1, 240)
    for job_id in range(2, 14):
        await sample(ws, job_id, 0)
    await astart_load_timing(ws.id, 100)
    estimate = await aload_time_estimate(ws.id, 100)
    assert estimate["usual_seconds"] == 240
    assert estimate["sample_count"] == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_bulk_estimates_keep_elapsed_times_and_modes_separate():
    ws = await make_workspace()
    await sample(ws, 1, 240)
    await astart_load_timing(ws.id, 100)
    await astart_load_timing(ws.id, 101)
    await astart_load_timing(ws.id, 102, only_unserved=True)
    now = timezone.now()
    for job_id, seconds in [(100, 60), (101, 120)]:
        await WorkspaceLoadTiming.objects.filter(job_id=job_id).aupdate(
            started_at=now - timedelta(seconds=seconds),
        )
    estimates = await aload_time_estimates(ws.id, {100, 101, 102}, now=now)
    assert estimates[100]["elapsed_seconds"] == 60
    assert estimates[101]["elapsed_seconds"] == 120
    assert estimates[102] is None


@pytest.mark.django_db(transaction=True)
def test_queued_job_uses_retained_load_mode_and_unknown_mode_has_no_estimate():
    user = User.objects.create_user(email="queued@example.test")
    ws = Workspace.objects.create(name="Queued", created_by=user)
    start = timezone.now() - timedelta(seconds=300)
    WorkspaceLoadTiming.objects.create(
        workspace=ws,
        job_id=999999,
        started_at=start,
        completed_at=start + timedelta(seconds=240),
        succeeded=True,
    )
    queued = {}
    with connection.cursor() as cursor:
        for mode in [False, True]:
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
                "VALUES ('default', 'apps.workspaces.tasks.materialize_workspace', 'todo', %s) "
                "RETURNING id",
                [json.dumps({"workspace_id": str(ws.id), "only_unserved": mode})],
            )
            queued[mode] = cursor.fetchone()[0]
    estimates = async_to_sync(aload_time_estimates)(ws.id, {*queued.values(), 999998})
    assert estimates[queued[False]]["usual_seconds"] == 240
    assert estimates[queued[False]]["elapsed_seconds"] is None
    assert estimates[queued[True]] is None
    assert estimates[999998] is None
