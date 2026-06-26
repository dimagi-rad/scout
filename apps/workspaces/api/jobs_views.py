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

# How far back to surface terminated ThreadJobs in the active-jobs response.
# The frontend polls active jobs every few seconds; this window lets the
# failure card render for users who were away from the tab when the
# materialization failed but have come back within the window.
RECENT_TERMINATION_WINDOW = timedelta(minutes=30)

# Minimum interval between API-side stale-job reconcile sweeps per workspace
# (arch #254, 05#6). The sweep is a backstop for a sick worker, not a per-poll
# duty: running its ~5 DB queries on every 3s poll multiplies platform-DB load
# with open tabs. Gating it to once per workspace per this interval keeps the
# backstop while removing it from the hot poll path. The frontend also pauses
# polling when the tab is hidden, so the two changes compound.
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
            # Display unit for the counts ("rows" for most sources; OCS
            # messages report per-session progress as "sessions").
            "unit": run_progress.get("unit") or "rows",
            "message": run_progress.get("message"),
            "source": run_progress.get("source"),
            "step": run_progress.get("step"),
            "total_steps": run_progress.get("total_steps"),
        }
    return {
        "thread_job_id": str(job.id),
        "thread_id": str(job.thread_id),
        # tool_call_id ties this job to the specific run_materialization
        # tool-call card in the chat transcript so the frontend can scope
        # progress and Stop affordances per-card rather than to every
        # historical run_materialization message.
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

    # ThreadJobs for this user's threads in this workspace, in active states.
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

    # Backstop for jobs the worker-side janitor should have reconciled but
    # couldn't — the janitor runs in the worker process, so a sick worker
    # (e.g. a permanently dead DB connection) strands jobs in active states
    # and the frontend spins forever. This poll runs in the API process and
    # can reconcile stale jobs itself: flip ones whose procrastinate job
    # failed, re-defer the resume for ones that quietly succeeded.
    #
    # Throttle the sweep per workspace (arch #254, 05#6): it's a backstop, not a
    # per-poll duty. Without this, every 3s poll runs the reconcile DB queries
    # for each stale job; gating to once per RECONCILE_THROTTLE_SECONDS removes
    # it from the hot path while still rescuing jobs a sick worker stranded.
    reconcile_gate_key = f"jobs_reconcile_gate:{workspace.id}"
    may_reconcile = await cache.aadd(reconcile_gate_key, 1, RECONCILE_THROTTLE_SECONDS)
    if may_reconcile:
        stale_cutoff = timezone.now() - workspace_tasks.STALE_JOB_THRESHOLD
        reconciled = False
        for tj in jobs:
            # Measure staleness from the resume phase for RUNNING jobs so a
            # healthy long-running resume (after a >10 min materialization) is
            # not falsely reconciled (finding 02#9); _staleness_anchor falls back
            # to created_at for PENDING / legacy rows.
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

    # Bulk-fetch the latest progress per procrastinate_job_id.
    job_ids = [j.procrastinate_job_id for j in jobs]
    runs_by_job: dict[int, dict] = {}
    async for r in MaterializationRun.objects.filter(
        procrastinate_job_id__in=job_ids,
    ).order_by("started_at"):
        # Last wins; per workspace this is "the currently active tenant_schema run".
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
