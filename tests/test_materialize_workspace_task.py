"""Tests for the procrastinate-backed materialize_workspace task and the
``/api/workspaces/<id>/materialization/cancel/`` endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.test import AsyncClient
from django.utils import timezone

from apps.chat.models import Thread, ThreadJob
from apps.users.models import TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceMembership,
    WorkspaceRole,
)
from apps.workspaces.tasks import _run_pipeline_with_progress, materialize_workspace
from mcp_server.services.materializer import MaterializationCancelled


def _mock_pipeline(provider="commcare", name="commcare_sync"):
    p = MagicMock()
    p.provider = provider
    p.name = name
    return p


def _mock_registry(provider="commcare"):
    pipeline = _mock_pipeline(provider=provider, name=f"{provider}_sync")
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline
    return registry


@pytest.fixture
def tenant_membership_obj(db, user, tenant):
    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


@pytest.fixture
def context_with_job_id():
    """Mock procrastinate JobContext with .job.id."""
    ctx = MagicMock()
    ctx.job.id = 42
    return ctx


# ---------------------------------------------------------------------------
# materialize_workspace task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_dispatches_per_tenant(
    workspace, tenant_membership_obj, context_with_job_id
):
    """The task resolves memberships and runs the pipeline once per tenant."""
    captured = {"calls": 0}

    def fake_pipeline_run(*args, **kwargs):
        captured["calls"] += 1
        return {"status": "completed", "rows_loaded": 7}

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", side_effect=fake_pipeline_run),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert captured["calls"] == 1
    assert result["all_succeeded"] is True
    assert result["tenants"][0]["success"] is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_records_failure(
    workspace, tenant_membership_obj, context_with_job_id
):
    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=RuntimeError("upstream API down"),
        ),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is False
    assert result["tenants"][0]["success"] is False
    assert "upstream API down" in result["tenants"][0]["error"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_breaks_on_cancel(
    workspace, tenant_membership_obj, context_with_job_id
):
    """When the pipeline raises MaterializationCancelled, processing stops."""
    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch(
            "apps.workspaces.tasks._run_pipeline_with_progress",
            side_effect=MaterializationCancelled(),
        ),
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        result = await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    assert result["all_succeeded"] is False
    assert result["tenants"][0]["cancelled"] is True


# ---------------------------------------------------------------------------
# _run_pipeline_with_progress closure
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_run_pipeline_with_progress_writes_progress_and_raises_on_cancel(
    db, tenant, tenant_membership_obj
):
    """The closure mirrors progress to the run row and raises when state==CANCELLED."""
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name="test_progress", state=SchemaState.ACTIVE
    )
    run = MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=99,
    )

    # Replace run_pipeline with a stub that invokes the updater with our
    # fixture run id, simulating run_pipeline's normal behavior.
    captured: list[dict] = []

    def fake_run_pipeline(*args, progress_updater=None, procrastinate_job_id=None, **kwargs):
        progress_updater(
            {
                "run_id": str(run.id),
                "step": 1,
                "total_steps": 4,
                "source": "cases",
                "message": "Loading...",
                "rows_loaded": 100,
                "rows_total": 1000,
            }
        )
        captured.append({"job_id": procrastinate_job_id})
        # Now flip to cancelled and call again — this should raise.
        MaterializationRun.objects.filter(id=run.id).update(
            state=MaterializationRun.RunState.CANCELLED
        )
        progress_updater(
            {
                "run_id": str(run.id),
                "step": 1,
                "total_steps": 4,
                "source": "cases",
                "message": "Loading...",
                "rows_loaded": 200,
                "rows_total": 1000,
            }
        )
        return {"status": "completed"}

    pipeline = _mock_pipeline()
    with (
        patch("apps.workspaces.tasks.run_pipeline", side_effect=fake_run_pipeline),
        pytest.raises(MaterializationCancelled),
    ):
        _run_pipeline_with_progress(
            tenant_membership_obj,
            {"type": "api_key", "value": "k"},
            pipeline,
            job_id=99,
        )

    # The updater writes progress on every call. The second call writes
    # rows_loaded=200 first, then re-reads state, sees CANCELLED, raises.
    run.refresh_from_db()
    assert run.progress is not None
    assert run.progress["rows_loaded"] == 200
    assert captured[0]["job_id"] == 99


# ---------------------------------------------------------------------------
# materialization cancel endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_marks_runs_cancelled(workspace, user, tenant):
    """POSTing cancels every active run for the workspace and aborts the job."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_cancel", state=SchemaState.ACTIVE
    )
    active_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=123,
    )
    # The refactored view routes through cancel_thread_job, which looks up
    # ThreadJobs by procrastinate_job_id. Create one so the run is cancelled.
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=123,
        tool_call_id="tc_legacy",
        state=ThreadJob.State.RUNNING,
    )

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")

    with patch("apps.workspaces.api.jobs_cancel.current_app") as mock_app:
        mock_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["runs_cancelled"] == 1
    mock_app.job_manager.cancel_job_by_id_async.assert_awaited_once_with(123, abort=True)

    await active_run.arefresh_from_db()
    assert active_run.state == MaterializationRun.RunState.CANCELLED
    assert active_run.completed_at is not None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_returns_no_active_run_when_idle(workspace, user, tenant):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_cancel_idle", state=SchemaState.ACTIVE
    )
    # A completed run shouldn't be touched.
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=timezone.now(),
    )

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")

    resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_active_run"
    assert body["runs_cancelled"] == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_endpoint_requires_workspace_membership(workspace, other_user):
    client = AsyncClient()
    await sync_to_async(client.login)(email=other_user.email, password="otherpass123")

    resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_materialize_workspace_chains_resume_task(
    workspace, user, tenant_membership_obj, context_with_job_id
):
    """When materialize_workspace finishes, it defers
    resume_thread_after_materialization for the matching ThreadJob."""
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tj = await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=context_with_job_id.job.id,
        tool_call_id="tc-chain",
    )

    with (
        patch("apps.workspaces.tasks.aresolve_credential", new_callable=AsyncMock) as mock_cred,
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry("commcare")),
        patch("apps.workspaces.tasks._run_pipeline_with_progress", return_value={"status": "ok"}),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume_mock,
    ):
        mock_cred.return_value = {"type": "api_key", "value": "k"}
        resume_mock.defer_async = AsyncMock(return_value=MagicMock(id=9999))
        await materialize_workspace(
            context_with_job_id,
            workspace_id=str(workspace.id),
            user_id="",
        )

    resume_mock.defer_async.assert_awaited_once()
    kwargs = resume_mock.defer_async.await_args.kwargs
    assert kwargs["thread_job_id"] == str(tj.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_legacy_cancel_handles_mixed_tracked_and_orphan_runs(workspace, user, tenant):
    """Mixed case: workspace has one tracked materialization (chat-triggered,
    with ThreadJob) and one orphan (e.g., /refresh/-triggered, no ThreadJob).
    The cancel endpoint must cancel BOTH runs."""
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_mixed_cancel", state=SchemaState.ACTIVE
    )

    # Tracked run: has a ThreadJob
    tracked_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=555,
    )
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await ThreadJob.objects.acreate(
        thread=thread,
        job_type="materialization",
        procrastinate_job_id=555,
        tool_call_id="tc-tracked",
        state=ThreadJob.State.RUNNING,
    )

    # Orphan run: no ThreadJob (e.g., /refresh/-initiated)
    orphan_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=666,
    )

    client = AsyncClient()
    await sync_to_async(client.login)(email=user.email, password="testpass123")

    with patch("apps.workspaces.api.jobs_cancel.current_app") as mock_tracked_app, \
         patch("apps.workspaces.api.materialization_views.current_app") as mock_orphan_app:
        mock_tracked_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        mock_orphan_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    # Both runs must be cancelled
    assert body["runs_cancelled"] == 2

    await tracked_run.arefresh_from_db()
    assert tracked_run.state == MaterializationRun.RunState.CANCELLED

    await orphan_run.arefresh_from_db()
    assert orphan_run.state == MaterializationRun.RunState.CANCELLED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_legacy_cancel_does_not_cancel_other_users_threadjob(
    workspace, user, other_user, tenant,
):
    """A workspace member must NOT be able to cancel another member's
    chat-driven materialization via the legacy
    /api/workspaces/<id>/materialization/cancel/ endpoint.

    The legacy endpoint resolves the workspace by membership, so a peer could
    previously sweep up ThreadJobs owned by another user — which then triggers
    a resume-task message in the victim's chat. The fix adds a thread__user
    filter so only the caller's own ThreadJobs are cancelled."""
    # Make other_user a member of the workspace (peer with access).
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=workspace, user=other_user, role=WorkspaceRole.READ_WRITE,
    )
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="test_xuser_cancel", state=SchemaState.ACTIVE,
    )
    # Active run owned by `user`'s thread.
    active_run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=777,
    )
    owner_thread = await Thread.objects.acreate(workspace=workspace, user=user)
    owner_tj = await ThreadJob.objects.acreate(
        thread=owner_thread,
        job_type="materialization",
        procrastinate_job_id=777,
        tool_call_id="tc-owner",
        state=ThreadJob.State.RUNNING,
    )

    # other_user (a workspace peer) attempts to cancel via the legacy endpoint.
    client = AsyncClient()
    await sync_to_async(client.login)(email=other_user.email, password="otherpass123")
    with patch("apps.workspaces.api.jobs_cancel.current_app") as mock_tracked_app, \
         patch("apps.workspaces.api.materialization_views.current_app") as mock_orphan_app:
        mock_tracked_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        mock_orphan_app.job_manager.cancel_job_by_id_async = AsyncMock(return_value=1)
        resp = await client.post(f"/api/workspaces/{workspace.id}/materialization/cancel/")

    # The endpoint may report "no_active_run" (no own ThreadJobs and no orphan
    # runs) — but for orphan-run discovery we'd cancel the workspace-scoped
    # run. The critical assertion is that the *owner*'s ThreadJob state is
    # NOT flipped to CANCELLED by the peer.
    assert resp.status_code == 200
    await sync_to_async(owner_tj.refresh_from_db)()
    assert owner_tj.state == ThreadJob.State.RUNNING, (
        "Peer must not be able to cancel another user's chat-driven ThreadJob"
    )
    # Implicit assertion: cancel_thread_job was never called for owner_tj, so
    # no resume task is queued against the victim's thread. (We cannot probe
    # the queue directly here, but state==RUNNING confirms the path didn't
    # run.)
    # active_run may be CANCELLED via the orphan fallback (workspace-scoped),
    # which is an acceptable per-finding trade-off; the security boundary is
    # that the victim's *chat* must not receive a cancellation message.
    await active_run.arefresh_from_db()
    assert active_run.state == MaterializationRun.RunState.CANCELLED
