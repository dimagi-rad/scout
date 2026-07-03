"""Tests for the stuck-MaterializationRun reconciler and job retention
(arch #255, 03#9 + 10#0) and the reconcile_stale_thread_job allowlist hardening.
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
)
from apps.workspaces.tasks import (
    JOB_RETENTION_HOURS,
    prune_old_procrastinate_jobs,
    reconcile_stale_materialization_runs,
    reconcile_stale_thread_job,
)

User = get_user_model()


async def _make_run(*, job_id, state=MaterializationRun.RunState.LOADING, with_threadjob=False):
    user = await User.objects.acreate_user(email=f"recon{job_id}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{job_id}", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id=f"t{job_id}", provider="commcare", canonical_name=f"T{job_id}"
    )
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"s_{job_id}", state=SchemaState.ACTIVE
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=state,
        procrastinate_job_id=job_id,
    )
    tj = None
    if with_threadjob:
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        tj = await ThreadJob.objects.acreate(
            thread=thread,
            job_type="materialization",
            procrastinate_job_id=job_id,
            tool_call_id=f"tc{job_id}",
            state=ThreadJob.State.PENDING,
        )
    return run, tj


# --- 03#9: stuck MaterializationRun reconciler --------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_fails_stalled_run_and_threadjob():
    """A LOADING run whose worker heartbeat is stale is a zombie: the reconciler
    flips it FAILED and fails its owning ThreadJob so the UI spinner clears."""
    run, tj = await _make_run(job_id=770001, with_threadjob=True)

    with (
        patch(
            "apps.workspaces.tasks._stalled_procrastinate_job_ids",
            new=AsyncMock(return_value={770001}),
        ),
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="doing"),  # zombie: doing but worker gone
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await reconcile_stale_materialization_runs()

    assert result == {"failed": 1}
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.FAILED
    assert run.completed_at is not None
    assert run.result.get("reconciled_stale") is True
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED
    assert tj.error_summary


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_leaves_live_run_untouched():
    """A run whose job is 'doing' with a LIVE heartbeat (not stalled) is a healthy
    long load — the reconciler must not touch it."""
    run, _ = await _make_run(job_id=770002)

    with (
        patch(
            "apps.workspaces.tasks._stalled_procrastinate_job_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="doing"),
        ),
    ):
        result = await reconcile_stale_materialization_runs()

    assert result == {"failed": 0}
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.LOADING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_fails_run_with_terminal_job():
    """A run stuck ACTIVE while its procrastinate job is already terminal
    (succeeded/failed) is a zombie the worker never closed out — fail it."""
    run, _ = await _make_run(job_id=770003)

    with (
        patch(
            "apps.workspaces.tasks._stalled_procrastinate_job_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="failed"),
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await reconcile_stale_materialization_runs()

    assert result == {"failed": 1}
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconciler_skips_when_status_unknown():
    """A transient status-read failure (None) must not fail the run this tick."""
    run, _ = await _make_run(job_id=770004)

    with (
        patch(
            "apps.workspaces.tasks._stalled_procrastinate_job_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await reconcile_stale_materialization_runs()

    assert result == {"failed": 0}
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.LOADING


# --- 10#0: allowlist hardening + retention ------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconcile_thread_job_skips_unknown_status():
    """An unrecognized/future procrastinate status must NOT fall into the resume
    act branch — the ThreadJob is left for the next tick."""
    _, tj = await _make_run(job_id=770005, with_threadjob=True)

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="some_future_status"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        action = await reconcile_stale_thread_job(tj)

    assert action is None
    resume.defer_async.assert_not_called()
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.PENDING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconcile_thread_job_skips_aborting_status():
    """'aborting' is transitional (abort requested, not yet acked) — treat it as
    in-flight, not terminal."""
    _, tj = await _make_run(job_id=770006, with_threadjob=True)

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="aborting"),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        action = await reconcile_stale_thread_job(tj)

    assert action is None
    resume.defer_async.assert_not_called()
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.PENDING


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_reconcile_thread_job_fails_on_cancelled_status():
    """A PENDING ThreadJob whose job is 'cancelled' has nothing to resume — flip
    it FAILED rather than deferring a resume (allowlist now includes cancelled)."""
    _, tj = await _make_run(job_id=770007, with_threadjob=True)

    with (
        patch(
            "apps.workspaces.tasks._procrastinate_job_status",
            new=AsyncMock(return_value="cancelled"),
        ),
        patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message",
            new=AsyncMock(return_value=None),
        ),
        patch("apps.workspaces.tasks.resume_thread_after_materialization") as resume,
    ):
        resume.defer_async = AsyncMock(return_value=None)
        action = await reconcile_stale_thread_job(tj)

    assert action == "failed"
    resume.defer_async.assert_not_called()
    await tj.arefresh_from_db()
    assert tj.state == ThreadJob.State.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prune_old_jobs_calls_delete_old_jobs_with_horizon():
    """The retention task prunes finalized jobs via delete_old_jobs at the
    configured horizon (succeeded-only default — no include_* flags)."""
    delete = AsyncMock(return_value=None)
    with patch("apps.workspaces.tasks.app") as mock_app:
        mock_app.job_manager.delete_old_jobs = delete
        result = await prune_old_procrastinate_jobs()

    assert result == {"pruned": True}
    delete.assert_awaited_once_with(nb_hours=JOB_RETENTION_HOURS)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_prune_old_jobs_degrades_gracefully_on_error():
    """A delete_old_jobs failure is best-effort — logged, not raised."""
    with patch("apps.workspaces.tasks.app") as mock_app:
        mock_app.job_manager.delete_old_jobs = AsyncMock(side_effect=RuntimeError("boom"))
        result = await prune_old_procrastinate_jobs()

    assert result == {"pruned": False}
