"""Async API views for ThreadJob status (polled by the frontend)."""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone

from apps.chat.models import ThreadJob
from apps.users.decorators import async_login_required
from apps.workspaces import tasks as workspace_tasks
from apps.workspaces.api.jobs_cancel import cancel_thread_job
from apps.workspaces.models import MaterializationRun
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)

# How far back to surface terminated ThreadJobs, so a failure card still renders
# for a user who returns to the tab shortly after a materialization failed.
RECENT_TERMINATION_WINDOW = timedelta(minutes=30)

# Min interval between API-side reconcile sweeps per workspace (arch #254, 05#6).
# The sweep is a sick-worker backstop, not a per-poll duty — gating it keeps its
# ~5 DB queries off the hot 3s poll path.
RECONCILE_THROTTLE_SECONDS = 30


def _job_to_dict(job: ThreadJob, run_progress: dict | None) -> dict:
    progress = None
    if run_progress:
        rows_loaded = run_progress.get("rows_loaded") or 0
        rows_total = run_progress.get("rows_total")
        percent = None
        if isinstance(rows_total, int) and rows_total > 0:
            percent = int(100 * rows_loaded / rows_total)
        progress = {
            "percent": percent,
            "rows_loaded": rows_loaded,
            "rows_total": rows_total,
            # "rows" for most sources; OCS reports per-session as "sessions".
            "unit": run_progress.get("unit") or "rows",
            "message": run_progress.get("message"),
            "source": run_progress.get("source"),
            "step": run_progress.get("step"),
            "total_steps": run_progress.get("total_steps"),
        }
    return {
        "thread_job_id": str(job.id),
        "thread_id": str(job.thread_id),
        # Ties this job to its run_materialization card so the frontend can scope
        # progress/Stop per-card, not to every historical run.
        "tool_call_id": job.tool_call_id,
        "job_type": job.job_type,
        "state": job.state,
        "progress": progress,
        "created_at": job.created_at.isoformat(),
    }


def _termination_to_dict(job: ThreadJob) -> dict:
    """Serialize a terminal ThreadJob for the ``recent_terminations`` payload.

    ``retry_available`` is True only for FAILED/CANCELLED — a COMPLETED job
    has no failure to retry from and we surface it in the payload only so the
    frontend can clear any stale failure card it had previously rendered.
    """
    return {
        "thread_job_id": str(job.id),
        "thread_id": str(job.thread_id),
        "tool_call_id": job.tool_call_id,
        "state": job.state,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_summary": job.error_summary or "",
        "retry_available": job.state
        in {
            ThreadJob.State.FAILED,
            ThreadJob.State.CANCELLED,
        },
    }


@async_login_required
async def active_jobs_view(request, workspace_id):
    """GET /api/workspaces/<workspace_id>/jobs/active/

    Returns ThreadJobs in non-terminal states for the current user, enriched
    with the latest MaterializationRun.progress. Also returns ThreadJobs that
    transitioned to a terminal state within RECENT_TERMINATION_WINDOW so the
    frontend can render an inline failure card for jobs that have already
    vanished from the active list. Polled by useWorkspaceJobs.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    def _fetch_active_jobs():
        return (
            ThreadJob.objects.select_related(
                "thread__workspace",
                "thread__user",
            )
            .filter(
                thread__workspace=workspace,
                thread__user=user,
                state__in=list(ThreadJob.ACTIVE_STATES),
            )
            .order_by("-created_at")
        )

    jobs = [j async for j in _fetch_active_jobs()]

    # Backstop for a sick worker whose janitor can't run: this API-process poll
    # reconciles stale jobs itself so the frontend doesn't spin forever. Throttled
    # per workspace (arch #254, 05#6) to keep the reconcile queries off the hot path.
    reconcile_gate_key = f"jobs_reconcile_gate:{workspace.id}"
    may_reconcile = await cache.aadd(reconcile_gate_key, 1, RECONCILE_THROTTLE_SECONDS)
    if may_reconcile:
        stale_cutoff = timezone.now() - workspace_tasks.STALE_JOB_THRESHOLD
        reconciled = False
        for tj in jobs:
            # Anchor on the resume phase for RUNNING jobs so a healthy long resume
            # isn't falsely reconciled (finding 02#9).
            if workspace_tasks._staleness_anchor(tj) >= stale_cutoff:
                continue
            try:
                action = await workspace_tasks.reconcile_stale_thread_job(tj)
            except Exception:
                logger.exception("active_jobs: reconcile failed for ThreadJob %s", tj.id)
                continue
            reconciled = reconciled or action is not None
        if reconciled:
            jobs = [j async for j in _fetch_active_jobs()]

    job_ids = [j.procrastinate_job_id for j in jobs]
    runs_by_job: dict[int, dict] = {}
    async for r in MaterializationRun.objects.filter(
        procrastinate_job_id__in=job_ids,
    ).order_by("started_at"):
        # Last wins — the currently active tenant_schema run.
        runs_by_job[r.procrastinate_job_id] = r.progress or {}

    cutoff = timezone.now() - RECENT_TERMINATION_WINDOW
    recent_terminations = [
        _termination_to_dict(j)
        async for j in ThreadJob.objects.filter(
            thread__workspace=workspace,
            thread__user=user,
            state__in=list(ThreadJob.TERMINAL_STATES),
            completed_at__gte=cutoff,
        ).order_by("-completed_at")
    ]

    return JsonResponse(
        {
            "jobs": [_job_to_dict(j, runs_by_job.get(j.procrastinate_job_id)) for j in jobs],
            "recent_terminations": recent_terminations,
        }
    )


@async_login_required
async def cancel_job_view(request, workspace_id, thread_job_id):
    """POST /api/workspaces/<workspace_id>/jobs/<thread_job_id>/cancel/"""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    try:
        tj = await ThreadJob.objects.aget(
            id=thread_job_id,
            thread__workspace=workspace,
            thread__user=user,
        )
    except ThreadJob.DoesNotExist:
        return JsonResponse({"error": "ThreadJob not found"}, status=404)

    if tj.state in ThreadJob.TERMINAL_STATES:
        return JsonResponse({"status": "already_terminal", "state": tj.state})

    runs_cancelled = await cancel_thread_job(tj)
    return JsonResponse({"status": "cancelled", "runs_cancelled": runs_cancelled})
