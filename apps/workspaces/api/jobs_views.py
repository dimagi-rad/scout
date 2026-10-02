"""Async API views for ThreadJob status (polled by the frontend)."""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone

from apps.chat.models import ThreadJob
from apps.chat.pending_requests import aworkspace_pending_requests
from apps.users.decorators import async_login_required
from apps.workspaces.access import (
    access_denied_response,
    aresolve_workspace_access_ex,
    role_satisfies,
)
from apps.workspaces.api.jobs_cancel import cancel_thread_job
from apps.workspaces.models import MaterializationRun, WorkspaceRole
from apps.workspaces.services import reconciliation
from apps.workspaces.services.failure_guidance import (
    BLOCKS_IMMEDIATE_RETRY,
    summary_failures,
)
from apps.workspaces.services.load_progress import aworkspace_load_progress, progress_payload
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)

# How far back to surface terminated ThreadJobs, so a failure card still renders
# for a user who returns to the tab shortly after a materialization failed.
RECENT_TERMINATION_WINDOW = timedelta(minutes=30)

# Min interval between API-side reconcile sweeps per workspace (arch #254, 05#6).
# The sweep is a sick-worker backstop, not a per-poll duty — gating it keeps its
# ~5 DB queries off the hot 3s poll path.
RECONCILE_THROTTLE_SECONDS = 30


def _job_to_dict(job: ThreadJob, run_progress: dict | None, load: dict | None = None) -> dict:
    return {
        "thread_job_id": str(job.id),
        "thread_id": str(job.thread_id),
        # Ties this job to its run_materialization card so the frontend can scope
        # progress/Stop per-card, not to every historical run.
        "tool_call_id": job.tool_call_id,
        "job_type": job.job_type,
        "state": job.state,
        "progress": progress_payload(run_progress),
        "source_index": load["source_index"] if load else None,
        "source_total": load["source_total"] if load else None,
        "tenant_name": load["tenant_name"] if load else None,
        "created_at": job.created_at.isoformat(),
    }


def _needs_materialization_retry_check(job: ThreadJob) -> bool:
    if job.state != ThreadJob.State.FAILED:
        return False
    if job.failure_phase in {ThreadJob.FailurePhase.RESUME, ThreadJob.FailurePhase.QUERY_BUILD}:
        return False
    # Historical jobs cannot distinguish a failed follow-up from failed loading.
    # Preserve Retry for those ambiguous resumed jobs rather than infer from prose.
    return bool(job.failure_phase) or job.started_at is None


def _termination_to_dict(
    job: ThreadJob, run_results: list[dict], *, viewer_can_write: bool
) -> dict:
    """Serialize a terminal ThreadJob for the ``recent_terminations`` payload.

    Retry stays available if it can recover any source or a failed follow-up.
    Completed jobs still clear stale failure cards in the frontend. The retry
    endpoint requires READ_WRITE, so a viewer without it never gets Retry,
    whatever the failure; one whose role was restored gets it back.
    """
    retry_available = job.state in {ThreadJob.State.FAILED, ThreadJob.State.CANCELLED}
    if (
        job.state == ThreadJob.State.FAILED
        and job.failure_phase == ThreadJob.FailurePhase.QUERY_BUILD
    ):
        retry_available = False
    elif _needs_materialization_retry_check(job):
        failures = summary_failures([*job.materialization_preflight_failures, *run_results])
        completed_source = any(
            isinstance(result, dict)
            and isinstance(result.get("sources"), dict)
            and any(
                isinstance(source, dict) and source.get("state") == "completed"
                for source in result["sources"].values()
            )
            for result in run_results
        )
        retry_available = (
            completed_source
            or not failures
            or any(f.code not in BLOCKS_IMMEDIATE_RETRY for f in failures)
        )
    return {
        "thread_job_id": str(job.id),
        "thread_id": str(job.thread_id),
        "tool_call_id": job.tool_call_id,
        "state": job.state,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_summary": job.error_summary or "",
        "retry_available": retry_available and viewer_can_write,
    }


@async_login_required
async def active_jobs_view(request, workspace_id):
    """GET /api/workspaces/<workspace_id>/jobs/active/

    Returns ThreadJobs in non-terminal states for the current user, enriched
    with the latest MaterializationRun.progress, plus ``workspace_loads``: every
    in-flight load of the workspace, whoever started it. Also returns ThreadJobs that
    transitioned to a terminal state within RECENT_TERMINATION_WINDOW so the
    frontend can render an inline failure card for jobs that have already
    vanished from the active list, and ``pending_requests``: the caller's requests
    held for a load, keyed by thread id. Polled by useWorkspaceJobs.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    # The full access result carries the viewer's role, which the retry policy needs.
    # Every job below is one the caller started from this workspace, so, as for MCP
    # status polling (#598), membership and coverage suffice: a 403 from a provider
    # verification outage would drop the user's own progress card mid-load.
    access = await aresolve_workspace_access_ex(user, workspace_id, verification=None)
    if not access.granted:
        return access_denied_response(access)
    workspace = access.workspace
    viewer_can_write = role_satisfies(access.membership.role, WorkspaceRole.READ_WRITE)

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
        stale_cutoff = timezone.now() - reconciliation.STALE_JOB_THRESHOLD
        reconciled = False
        for tj in jobs:
            # Anchor on the resume phase for RUNNING jobs so a healthy long resume
            # isn't falsely reconciled (finding 02#9).
            if reconciliation.staleness_anchor(tj) >= stale_cutoff:
                continue
            try:
                action = await reconciliation.reconcile_stale_thread_job(tj)
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

    workspace_loads = await aworkspace_load_progress(workspace)
    loads_by_job = {
        load["procrastinate_job_id"]: load
        for load in workspace_loads
        if load["procrastinate_job_id"] is not None
    }

    cutoff = timezone.now() - RECENT_TERMINATION_WINDOW
    terminated_jobs = [
        j
        async for j in ThreadJob.objects.filter(
            thread__workspace=workspace,
            thread__user=user,
            state__in=list(ThreadJob.TERMINAL_STATES),
            completed_at__gte=cutoff,
        ).order_by("-completed_at")
    ]
    results_by_job: dict[int, list[dict]] = {}
    async for run in MaterializationRun.objects.filter(
        procrastinate_job_id__in=[
            job.procrastinate_job_id
            for job in terminated_jobs
            # Retry is hidden from non-writers anyway; skip the scan on their polls.
            if viewer_can_write and _needs_materialization_retry_check(job)
        ],
    ).only("procrastinate_job_id", "result"):
        if isinstance(run.result, dict):
            results_by_job.setdefault(run.procrastinate_job_id, []).append(run.result)
    recent_terminations = [
        _termination_to_dict(
            job,
            results_by_job.get(job.procrastinate_job_id, []),
            viewer_can_write=viewer_can_write,
        )
        for job in terminated_jobs
    ]

    return JsonResponse(
        {
            "jobs": [
                _job_to_dict(
                    j,
                    runs_by_job.get(j.procrastinate_job_id),
                    loads_by_job.get(j.procrastinate_job_id),
                )
                for j in jobs
            ],
            "workspace_loads": workspace_loads,
            "recent_terminations": recent_terminations,
            "pending_requests": await aworkspace_pending_requests(workspace, user),
        }
    )


@async_login_required
async def cancel_job_view(request, workspace_id, thread_job_id):
    """POST /api/workspaces/<workspace_id>/jobs/<thread_job_id>/cancel/"""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    # Stopping work only reduces protected activity, so it stays reachable while
    # upstream verification is failing (recovery mode: membership and role only).
    workspace, err = await aresolve_workspace(
        user, workspace_id, minimum_role=WorkspaceRole.READ_WRITE, verification=None
    )
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
