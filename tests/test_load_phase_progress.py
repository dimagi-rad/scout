"""Post-load phases on the chat's progress card (building, combining, data model, answering)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.load_phases import LoadPhase
from apps.workspaces.services.materialize import _apublish_phase
from tests.tenant_access import ausable_connection

User = get_user_model()

JOB_ID = 4242


async def _job_with_run(state=ThreadJob.State.PENDING, progress=None):
    user = await User.objects.acreate_user(email="phase@load.test", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    tenant = await Tenant.objects.acreate(
        external_id="t-phase", provider="commcare", canonical_name="Phase"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_phase")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=JOB_ID,
        tool_call_id="tc-phase",
        state=state,
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=JOB_ID,
        progress=progress
        if progress is not None
        else {
            "step": 5,
            "total_steps": 5,
            "source": "cases",
            "message": "Loading cases...",
            "rows_loaded": 900,
            "rows_total": 1000,
        },
    )
    client = AsyncClient()
    await client.alogin(email="phase@load.test", password="x")
    return ws, run, client


async def _progress(client, ws):
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    [job] = resp.json()["jobs"]
    return job["progress"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_post_load_phase_reaches_the_card_after_the_run_completed():
    ws, _run, client = await _job_with_run()

    await _apublish_phase(JOB_ID, LoadPhase.BUILDING_MODEL, "Preparing the data model")

    progress = await _progress(client, ws)
    assert progress["phase"] == "building_model"
    assert progress["message"] == "Preparing the data model"
    # The finished source's counts must not linger under the new phase.
    assert progress["source"] is None
    assert progress["rows_loaded"] == 0
    assert progress["percent"] is None
    assert progress["step"] == 5
    assert progress["total_steps"] == 5


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_phase_is_written_to_the_jobs_latest_run():
    ws, first, client = await _job_with_run()
    second = await MaterializationRun.objects.acreate(
        tenant_schema=first.tenant_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        procrastinate_job_id=JOB_ID,
        progress={"step": 2, "total_steps": 4},
    )

    await _apublish_phase(JOB_ID, LoadPhase.COMBINING_SITES, "Joining 2 sources")

    await first.arefresh_from_db()
    await second.arefresh_from_db()
    assert "phase" not in first.progress
    assert second.progress["phase"] == "combining_sites"
    assert (await _progress(client, ws))["phase"] == "combining_sites"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_publish_phase_without_a_job_or_run_is_a_no_op():
    await _apublish_phase(None, LoadPhase.FINISHING, "x")
    await _apublish_phase(999_999, LoadPhase.FINISHING, "x")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_loading_progress_has_no_phase():
    ws, _run, client = await _job_with_run()

    progress = await _progress(client, ws)

    assert progress["phase"] is None
    assert progress["source"] == "cases"
    assert progress["percent"] == 90


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_running_job_reports_answering_over_the_runs_last_phase():
    ws, _run, client = await _job_with_run(
        state=ThreadJob.State.RUNNING,
        progress={"phase": "finishing", "message": "Updating views", "step": 5, "total_steps": 5},
    )

    progress = await _progress(client, ws)

    assert progress["phase"] == "answering"
    assert progress["source"] is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_running_job_with_no_run_still_reports_answering():
    ws, run, client = await _job_with_run(state=ThreadJob.State.RUNNING)
    await run.adelete()

    progress = await _progress(client, ws)

    assert progress["phase"] == "answering"
