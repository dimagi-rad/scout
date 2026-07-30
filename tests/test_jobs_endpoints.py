import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import AsyncClient
from django.utils import timezone
from procrastinate.contrib.django.procrastinate_app import FutureApp
from procrastinate.manager import JobManager

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.api import jobs_cancel
from apps.workspaces.api.jobs_cancel import cancel_thread_job
from apps.workspaces.models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.tasks import reconcile_stale_thread_job

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_returns_pending_job_with_progress():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    tenant = await Tenant.objects.acreate(
        external_id="t1",
        provider="commcare",
        canonical_name="Test Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(user=user, tenant=tenant)  # live-tenant access gate
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="s_t1",
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=1001,
        tool_call_id="tc1",
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=1001,
        progress={
            "step": 3,
            "total_steps": 5,
            "source": "cases",
            "message": "Loading cases...",
            "rows_loaded": 64000,
            "rows_total": 100000,
            "run_id": str(schema.id),
        },
    )
    client = AsyncClient()
    await client.alogin(email="a@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    j = body["jobs"][0]
    assert j["thread_id"] == str(thread.id)
    assert j["state"] == "pending"
    assert j["progress"]["percent"] == 64
    assert j["progress"]["rows_loaded"] == 64000
    # tool_call_id is exposed so the frontend can scope the progress card
    # to the specific run_materialization tool-call message in the chat.
    assert j["tool_call_id"] == "tc1"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_includes_recent_terminations():
    """Recently-terminated ThreadJobs surface in the response so the frontend
    can render an inline failure card once the spinner clears."""
    user = await User.objects.acreate_user(email="rt@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    failed_tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=5001,
        tool_call_id="tc-failed",
        state=ThreadJob.State.FAILED,
        error_summary="completed_works failed: HTTP 500",
    )
    # Set completed_at within the window
    await ThreadJob.objects.filter(id=failed_tj.id).aupdate(completed_at=timezone.now())

    client = AsyncClient()
    await client.alogin(email="rt@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert len(body["recent_terminations"]) == 1
    rt = body["recent_terminations"][0]
    assert rt["thread_job_id"] == str(failed_tj.id)
    assert rt["state"] == "failed"
    assert rt["error_summary"] == "completed_works failed: HTTP 500"
    assert rt["retry_available"] is True
    assert rt["tool_call_id"] == "tc-failed"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recent_terminations_filtered_by_user():
    """A terminated ThreadJob in another user's thread must not leak."""
    me = await User.objects.acreate_user(email="me@b.c", password="x")
    other = await User.objects.acreate_user(email="other@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=me)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=me,
        role=WorkspaceRole.READ_WRITE,
    )
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=other,
        role=WorkspaceRole.READ_WRITE,
    )
    other_thread = await Thread.objects.acreate(workspace=ws, user=other)
    tj = await ThreadJob.objects.acreate(
        thread=other_thread,
        job_type="materialization",
        procrastinate_job_id=5002,
        tool_call_id="tc-other",
        state=ThreadJob.State.FAILED,
        error_summary="should not leak",
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(completed_at=timezone.now())

    client = AsyncClient()
    await client.alogin(email="me@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    assert resp.json()["recent_terminations"] == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recent_terminations_filtered_by_window():
    """Terminations older than RECENT_TERMINATION_WINDOW are excluded."""
    user = await User.objects.acreate_user(email="window@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=5003,
        tool_call_id="tc-old",
        state=ThreadJob.State.FAILED,
    )
    # Backdate completed_at past the 30-minute window.
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        completed_at=timezone.now() - timedelta(hours=2),
    )

    client = AsyncClient()
    await client.alogin(email="window@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    assert resp.json()["recent_terminations"] == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recent_terminations_completed_state_has_no_retry():
    """COMPLETED state surfaces in the payload (so a stale failure card can be
    cleared) but ``retry_available`` is False."""
    user = await User.objects.acreate_user(email="ok@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=5004,
        tool_call_id="tc-ok",
        state=ThreadJob.State.COMPLETED,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(completed_at=timezone.now())

    client = AsyncClient()
    await client.alogin(email="ok@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    body = resp.json()
    assert len(body["recent_terminations"]) == 1
    assert body["recent_terminations"][0]["retry_available"] is False
    assert body["recent_terminations"][0]["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_retry_endpoint_dispatches_new_materialization():
    user = await User.objects.acreate_user(email="retry@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)

    client = AsyncClient()
    await client.alogin(email="retry@b.c", password="x")
    fake_job = type("J", (), {"id": 5005})()
    with patch("apps.workspaces.api.materialization_views.materialize_workspace") as mock_task:
        mock_task.defer_async = AsyncMock(return_value=fake_job)
        resp = await client.post(
            f"/api/workspaces/{ws.id}/materialize/retry/",
            data=json.dumps({"thread_id": str(thread.id), "tool_call_id": "tc-retry"}),
            content_type="application/json",
        )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "started"
    assert "thread_job_id" in body
    # A fresh ThreadJob is bound to the original thread so the resume
    # mechanism fires when the new run finishes.
    tj = await ThreadJob.objects.aget(id=body["thread_job_id"])
    assert tj.thread_id == thread.id
    assert tj.procrastinate_job_id == 5005
    assert tj.state == ThreadJob.State.PENDING
    assert tj.tool_call_id == "tc-retry"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_retry_endpoint_dedupes_in_flight():
    """If a materialization is already running for this thread, retry returns
    the existing ThreadJob identity instead of dispatching a duplicate."""
    user = await User.objects.acreate_user(email="dedupe@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    existing = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=5006,
        tool_call_id="tc-existing",
        state=ThreadJob.State.RUNNING,
    )

    client = AsyncClient()
    await client.alogin(email="dedupe@b.c", password="x")
    with patch("apps.workspaces.api.materialization_views.materialize_workspace") as mock_task:
        mock_task.defer_async = AsyncMock()
        resp = await client.post(
            f"/api/workspaces/{ws.id}/materialize/retry/",
            data=json.dumps({"thread_id": str(thread.id)}),
            content_type="application/json",
        )
        # No new dispatch when there's an in-flight job
        mock_task.defer_async.assert_not_called()
    body = resp.json()
    assert body["status"] == "already_in_progress"
    assert body["thread_job_id"] == str(existing.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_retry_endpoint_rejects_cross_user_thread():
    """The supplied thread_id must belong to the caller in the workspace."""
    me = await User.objects.acreate_user(email="r-me@b.c", password="x")
    other = await User.objects.acreate_user(email="r-other@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=me)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=me,
        role=WorkspaceRole.READ_WRITE,
    )
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=other,
        role=WorkspaceRole.READ_WRITE,
    )
    other_thread = await Thread.objects.acreate(workspace=ws, user=other)

    client = AsyncClient()
    await client.alogin(email="r-me@b.c", password="x")
    resp = await client.post(
        f"/api/workspaces/{ws.id}/materialize/retry/",
        data=json.dumps({"thread_id": str(other_thread.id)}),
        content_type="application/json",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_empty_when_none_running():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    client = AsyncClient()
    await client.alogin(email="a@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": [], "recent_terminations": []}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_unauthenticated_returns_401_or_403():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    client = AsyncClient()
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_non_member_blocked():
    owner = await User.objects.acreate_user(email="o@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=owner)
    await User.objects.acreate_user(email="out@b.c", password="x")
    client = AsyncClient()
    await client.alogin(email="out@b.c", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_flips_state_and_aborts_procrastinate():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    tenant = await Tenant.objects.acreate(
        external_id="t1",
        provider="commcare",
        canonical_name="Test Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(user=user, tenant=tenant)  # live-tenant access gate
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_t1")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=2002,
        tool_call_id="tc2",
        state=ThreadJob.State.RUNNING,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=2002,
    )
    client = AsyncClient()
    await client.alogin(email="a@b.c", password="x")

    with patch("apps.workspaces.api.jobs_cancel.app") as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=None)
        resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.CANCELLED
    run = await MaterializationRun.objects.aget(procrastinate_job_id=2002)
    assert run.state == MaterializationRun.RunState.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_cross_user_blocked():
    owner = await User.objects.acreate_user(email="o@b.c", password="x")
    other = await User.objects.acreate_user(email="x@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=owner)
    # Both users are workspace members so aresolve_workspace lets them through.
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=owner,
        role=WorkspaceRole.READ_WRITE,
    )
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=other,
        role=WorkspaceRole.READ,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=owner)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=3030,
        tool_call_id="tcX",
        state=ThreadJob.State.RUNNING,
    )
    client = AsyncClient()
    await client.alogin(email="x@b.c", password="x")
    resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 404
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.RUNNING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_job_double_cancel_is_idempotent():
    user = await User.objects.acreate_user(email="a@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=4040,
        tool_call_id="tc4",
        state=ThreadJob.State.CANCELLED,
    )
    client = AsyncClient()
    await client.alogin(email="a@b.c", password="x")
    resp = await client.post(f"/api/workspaces/{ws.id}/jobs/{tj.id}/cancel/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_terminal", "state": "cancelled"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_does_not_overwrite_terminal_threadjob():
    """If a ThreadJob has already reached a terminal state (e.g., resume
    finished concurrently), cancel must not overwrite it."""
    user = await User.objects.acreate_user(email="cancel-race@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-race", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=9090,
        tool_call_id="tc-race",
        state=ThreadJob.State.COMPLETED,  # Already terminal — resume just finished
    )
    # Call cancel_thread_job directly to exercise the race window
    with patch("apps.workspaces.api.jobs_cancel.app") as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=None)
        await cancel_thread_job(tj)

    await tj.arefresh_from_db()
    # State must remain COMPLETED, not be overwritten with CANCELLED
    assert tj.state == ThreadJob.State.COMPLETED


# ── Progress banner / total_rows tests ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_exposes_rows_total_for_percentage():
    """When the MaterializationRun.progress carries rows_total (from Connect's
    first-page ``count`` field), the active-jobs endpoint surfaces it so the
    frontend can compute and display a percentage.
    """
    user = await User.objects.acreate_user(email="total@rows.test", password="x")
    ws = await Workspace.objects.acreate(name="WTotal", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    tenant = await Tenant.objects.acreate(
        external_id="ccc-opp-999",
        provider="commcare_connect",
        canonical_name="Connect Test",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(user=user, tenant=tenant)  # live-tenant access gate
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_ccc_999")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=7777,
        tool_call_id="tc-total",
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="connect_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=7777,
        progress={
            "run_id": str(schema.id),
            "step": 3,
            "total_steps": 9,
            "source": "visits",
            "message": "Loading visits from commcare_connect API...",
            "rows_loaded": 25000,
            "rows_total": 50000,
        },
    )

    client = AsyncClient()
    await client.alogin(email="total@rows.test", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1

    p = body["jobs"][0]["progress"]
    # rows_total is exposed so the frontend banner can show "25,000 / 50,000 rows"
    assert p["rows_total"] == 50000
    assert p["rows_loaded"] == 25000
    # percent is pre-computed server-side (50%)
    assert p["percent"] == 50
    assert p["source"] == "visits"
    # Progress dicts written before the unit field existed default to "rows".
    assert p["unit"] == "rows"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_passes_through_progress_unit():
    """OCS messages report progress in sessions (issue #221); the unit is
    surfaced so the banner can label the counts honestly."""
    user = await User.objects.acreate_user(email="unit@rows.test", password="x")
    ws = await Workspace.objects.acreate(name="WUnit", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    tenant = await Tenant.objects.acreate(
        external_id="exp-uuid-unit",
        provider="ocs",
        canonical_name="OCS Test",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(user=user, tenant=tenant)  # live-tenant access gate
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_ocs_unit")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=7778,
        tool_call_id="tc-unit",
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="ocs_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=7778,
        progress={
            "run_id": str(schema.id),
            "step": 4,
            "total_steps": 7,
            "source": "messages",
            "message": "Loading messages from ocs API...",
            "rows_loaded": 120,
            "rows_total": 480,
            "unit": "sessions",
        },
    )

    client = AsyncClient()
    await client.alogin(email="unit@rows.test", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    p = body["jobs"][0]["progress"]
    assert p["unit"] == "sessions"
    assert p["percent"] == 25


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_percent_null_when_rows_total_missing():
    """When rows_total is absent (non-Connect providers or very first page),
    percent must be null rather than a division-by-zero error.
    """
    user = await User.objects.acreate_user(email="nopct@rows.test", password="x")
    ws = await Workspace.objects.acreate(name="WNoPct", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    tenant = await Tenant.objects.acreate(
        external_id="cc-domain-x",
        provider="commcare",
        canonical_name="CommCare Test",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(user=user, tenant=tenant)  # live-tenant access gate
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_cc_x")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=8888,
        tool_call_id="tc-nopct",
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=8888,
        progress={
            "run_id": str(schema.id),
            "step": 2,
            "total_steps": 5,
            "source": "cases",
            "message": "Loading cases...",
            "rows_loaded": 3200,
            "rows_total": None,
        },
    )

    client = AsyncClient()
    await client.alogin(email="nopct@rows.test", password="x")
    resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
    assert resp.status_code == 200
    body = resp.json()
    p = body["jobs"][0]["progress"]
    assert p["rows_loaded"] == 3200
    assert p["rows_total"] is None
    # percent must be null — not zero, not a division error
    assert p["percent"] is None


# ---------------------------------------------------------------------------
# API-side stale-job backstop
#
# The janitor task runs in the worker process; when the worker itself is sick
# (June 2026 incident: its DB connection died and every task — including the
# janitor — failed for ~22h) nothing flips stuck ThreadJobs and the frontend
# spins on "Preparing…" forever. The polling endpoint runs in the healthy API
# process, so it reconciles stale active jobs itself before responding.
# ---------------------------------------------------------------------------


async def _make_stale_pending_job(email: str, job_id: int) -> tuple:
    user = await User.objects.acreate_user(email=email, password="x")
    ws = await Workspace.objects.acreate(name=f"W-{job_id}", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=job_id,
        tool_call_id=f"tc-{job_id}",
        state=ThreadJob.State.PENDING,
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        created_at=timezone.now() - timedelta(hours=2),
    )
    return user, ws, tj


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_flips_stale_job_whose_procrastinate_job_failed():
    """A stale PENDING ThreadJob whose underlying procrastinate job FAILED is
    flipped to FAILED during the poll and surfaces in recent_terminations with
    an error_summary — the frontend just renders what the backend reports."""
    _user, ws, tj = await _make_stale_pending_job("stalefail@b.c", 50001)

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="failed"),
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
    ):
        client = AsyncClient()
        await client.alogin(email="stalefail@b.c", password="x")
        resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    assert len(body["recent_terminations"]) == 1
    term = body["recent_terminations"][0]
    assert term["thread_job_id"] == str(tj.id)
    assert term["state"] == "failed"
    assert term["retry_available"] is True
    assert term["error_summary"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_defers_resume_for_stale_job_whose_procrastinate_job_succeeded():
    """Stale PENDING job whose materialization actually finished: the poll
    defers the resume task (same semantics as the janitor) and keeps the job
    in the active list — the resume flips the state."""
    _user, ws, tj = await _make_stale_pending_job("stalesucc@b.c", 50002)

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        client = AsyncClient()
        await client.alogin(email="stalesucc@b.c", password="x")
        resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")

    assert resp.status_code == 200
    body = resp.json()
    resume.defer_async.assert_awaited_once_with(thread_job_id=str(tj.id))
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_does_not_reconcile_fresh_jobs():
    """Jobs younger than the staleness threshold are returned untouched —
    the backstop must not add a procrastinate status query to every poll of
    a healthy in-flight materialization."""
    user = await User.objects.acreate_user(email="fresh@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-fresh", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=50003,
        tool_call_id="tc-50003",
        state=ThreadJob.State.PENDING,
    )

    with patch(
        "apps.workspaces.tasks._procrastinate_job_status",
        new=AsyncMock(return_value="failed"),
    ) as status_mock:
        client = AsyncClient()
        await client.alogin(email="fresh@b.c", password="x")
        resp = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")

    assert resp.status_code == 200
    body = resp.json()
    status_mock.assert_not_called()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["thread_job_id"] == str(tj.id)
    assert body["jobs"][0]["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_jobs_throttles_reconcile_across_rapid_polls():
    """The stale-job reconcile sweep is throttled per-workspace so a 3s poll
    loop doesn't run ~5 reconcile DB queries on every tick (arch #254, 05#6).
    The first poll reconciles; a rapid second poll skips the sweep.
    """
    cache.clear()
    _user, ws, _tj = await _make_stale_pending_job("throttle@b.c", 50009)

    with patch(
        "apps.workspaces.api.jobs_views.workspace_tasks.reconcile_stale_thread_job",
        new=AsyncMock(return_value=None),
    ) as reconcile_mock:
        client = AsyncClient()
        await client.alogin(email="throttle@b.c", password="x")
        await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
        first_calls = reconcile_mock.await_count
        await client.get(f"/api/workspaces/{ws.id}/jobs/active/")
        second_calls = reconcile_mock.await_count

    assert first_calls >= 1, "first poll should reconcile the stale job"
    assert second_calls == first_calls, "rapid second poll must skip the reconcile sweep"


# ---------------------------------------------------------------------------
# 12#0 item 1: the RUNNING false-failure reconcile branch
#
# Every other stale-reconcile test builds a PENDING ThreadJob, so
# reconcile_stale_thread_job's RUNNING branch (worker started a resume then
# crashed) was never exercised. Build it explicitly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconcile_flips_stale_running_job_straight_to_failed():
    """A stale RUNNING ThreadJob whose procrastinate job is no longer running
    (a worker claimed the resume then crashed mid-ainvoke) must be flipped
    straight to FAILED with the interrupted-resume summary — NOT handed a
    duplicate resume that could race a still-running first invocation."""
    user = await User.objects.acreate_user(email="stalerun@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-run", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=60001,
        tool_call_id="tc-run",
        state=ThreadJob.State.RUNNING,
    )
    # A RUNNING job's staleness is measured from the RESUME phase (started_at),
    # not created_at (finding 02#9). Backdate started_at so the resume reads as
    # genuinely stuck rather than freshly started.
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        started_at=timezone.now() - timedelta(hours=2),
    )
    await tj.arefresh_from_db()

    with (
        # Terminal procrastinate status (succeeded) — NOT todo/doing — so the
        # reconcile proceeds; tj.state==RUNNING selects the worker-crash branch.
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="succeeded"),
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ) as persist,
    ):
        action = await reconcile_stale_thread_job(tj)

    assert action == "failed"
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.completed_at is not None
    # The summary tells the user the follow-up was interrupted and to retry —
    # it must NOT claim the materialization itself failed.
    assert "interrupted" in tj.error_summary.lower()
    assert "retry" in tj.error_summary.lower()
    # The crash is surfaced to the user via a synthetic message.
    persist.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconcile_leaves_fresh_running_resume_alone_even_when_job_terminal():
    """The 02#9 anti-false-positive guard: a RUNNING ThreadJob whose resume only
    just started (fresh started_at) must be left alone even though its
    materialization job long since succeeded — a healthy resume after a long
    materialization must NOT be mistaken for a crash and falsely failed."""
    user = await User.objects.acreate_user(email="runfresh@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-fresh-run", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=60002,
        tool_call_id="tc-fresh-run",
        state=ThreadJob.State.RUNNING,
    )
    # Resume just started — well within the staleness threshold.
    await ThreadJob.objects.filter(id=tj.id).aupdate(started_at=timezone.now())
    await tj.arefresh_from_db()

    with patch(
        "apps.workspaces.tasks._procrastinate_job_status",
        new=AsyncMock(return_value="succeeded"),
    ):
        action = await reconcile_stale_thread_job(tj)

    assert action is None
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.RUNNING


# ---------------------------------------------------------------------------
# 12#0 item 2 / arch #255 02#5: exercise the REAL app binding in cancel_thread_job
#
# jobs_cancel now imports the lazy ProxyApp (not an import-time FutureApp binding),
# so an import-order regression can't silently disable the abort (arch #255 02#5).
#
# These tests leave the binding intact and patch the abort at the JobManager
# CLASS level, so the real binding must resolve for the abort to fire.
# ---------------------------------------------------------------------------


def test_jobs_cancel_app_binding_is_live_not_a_blueprint():
    """The abort binding must resolve to a real App exposing a usable
    job_manager — not procrastinate's not-ready FutureApp blueprint."""
    assert not isinstance(jobs_cancel.app, FutureApp)
    job_manager = jobs_cancel.app.job_manager
    assert hasattr(job_manager, "cancel_job_by_id_async")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_thread_job_aborts_through_live_current_app_binding():
    """cancel_thread_job must actually send the procrastinate abort through its
    real current_app binding. Patching JobManager.cancel_job_by_id_async at the
    class level (instead of the module binding) means a regression to a stale
    binding makes job_manager access raise, the mock is never called, and this
    fails loudly."""
    user = await User.objects.acreate_user(email="livebind@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-bind", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    tenant = await Tenant.objects.acreate(
        external_id="t-bind",
        provider="commcare",
        canonical_name="Bind Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name="s_bind")
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=61001,
        tool_call_id="tc-bind",
        state=ThreadJob.State.RUNNING,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=61001,
    )

    with patch.object(
        JobManager,
        "cancel_job_by_id_async",
        new=AsyncMock(return_value=None),
    ) as mock_abort:
        runs_cancelled = await cancel_thread_job(tj)

    # The abort fired through the live binding with the right job id + abort flag.
    mock_abort.assert_awaited_once_with(tj.procrastinate_job_id, abort=True)
    assert runs_cancelled == 1
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.CANCELLED
    run = await MaterializationRun.objects.aget(procrastinate_job_id=61001)
    assert run.state == MaterializationRun.RunState.CANCELLED
