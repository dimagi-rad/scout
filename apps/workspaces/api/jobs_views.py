"""Async API views for ThreadJob status (polled by the frontend)."""

import logging

from django.http import JsonResponse

from apps.chat.models import ThreadJob
from apps.users.decorators import async_login_required
from apps.workspaces.api.jobs_cancel import cancel_thread_job
from apps.workspaces.models import MaterializationRun
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)


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


@async_login_required
async def active_jobs_view(request, workspace_id):
    """GET /api/workspaces/<workspace_id>/jobs/active/

    Returns ThreadJobs in non-terminal states for the current user, enriched
    with the latest MaterializationRun.progress. Polled by useWorkspaceJobs.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    # ThreadJobs for this user's threads in this workspace, in active states.
    jobs = [
        j
        async for j in ThreadJob.objects.filter(
            thread__workspace=workspace,
            thread__user=user,
            state__in=list(ThreadJob.ACTIVE_STATES),
        ).order_by("-created_at")
    ]

    # Bulk-fetch the latest progress per procrastinate_job_id.
    job_ids = [j.procrastinate_job_id for j in jobs]
    runs_by_job: dict[int, dict] = {}
    async for r in MaterializationRun.objects.filter(
        procrastinate_job_id__in=job_ids,
    ).order_by("started_at"):
        # Last wins; per workspace this is "the currently active tenant_schema run".
        runs_by_job[r.procrastinate_job_id] = r.progress or {}

    return JsonResponse(
        {"jobs": [_job_to_dict(j, runs_by_job.get(j.procrastinate_job_id)) for j in jobs]}
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
