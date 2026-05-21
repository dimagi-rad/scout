"""Async API views for materialization lifecycle (cancel)."""

import logging
from datetime import UTC, datetime

from django.http import JsonResponse
from procrastinate.contrib.django.procrastinate_app import current_app

from apps.chat.models import ThreadJob
from apps.users.decorators import async_login_required
from apps.workspaces.api.jobs_cancel import cancel_thread_job
from apps.workspaces.models import MaterializationRun
from apps.workspaces.workspace_resolver import aresolve_workspace

logger = logging.getLogger(__name__)


@async_login_required
async def materialization_cancel_view(request, workspace_id):
    """POST /api/workspaces/<workspace_id>/materialization/cancel/

    Marks every active ``MaterializationRun`` for this workspace as
    CANCELLED so the in-process worker observes the cancellation between
    pages, and signals procrastinate to cancel/abort the underlying jobs.

    The DB state must be flipped *before* signalling procrastinate — the
    worker's ``progress_updater`` checks the run state on every page;
    procrastinate's abort signal only takes effect at the next ``await``
    boundary (and our work runs inside ``asyncio.to_thread``, so it never
    sees one).

    Delegates to ``cancel_thread_job`` for each matching ThreadJob so that
    both the run state and the job state are flipped atomically.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    active_runs = [
        r
        async for r in MaterializationRun.objects.select_related(
            "tenant_schema__tenant"
        ).filter(
            tenant_schema__tenant__in=workspace.tenants.all(),
            state__in=list(MaterializationRun.ACTIVE_STATES),
        )
    ]
    if not active_runs:
        return JsonResponse({"status": "no_active_run", "runs_cancelled": 0})

    job_ids = {r.procrastinate_job_id for r in active_runs if r.procrastinate_job_id is not None}
    tjs = [
        tj
        async for tj in ThreadJob.objects.filter(
            procrastinate_job_id__in=job_ids,
            state__in=list(ThreadJob.ACTIVE_STATES),
        )
    ]
    # Cancel tracked materializations via cancel_thread_job (which also flips
    # the ThreadJob state). Track which procrastinate_job_ids we covered.
    total = 0
    tracked_job_ids = set()
    for tj in tjs:
        total += await cancel_thread_job(tj)
        tracked_job_ids.add(tj.procrastinate_job_id)

    # Cancel any orphan MaterializationRuns (no ThreadJob — usually /refresh/-initiated
    # materializations that go through run_pipeline directly).
    orphan_job_ids = job_ids - tracked_job_ids
    if orphan_job_ids:
        logger.info(
            "materialization_cancel_view: %d orphan run(s) without ThreadJob — "
            "falling back to direct cancellation",
            len(orphan_job_ids),
        )
        now = datetime.now(UTC)
        orphan_run_ids = [r.id for r in active_runs if r.procrastinate_job_id in orphan_job_ids]
        orphan_cancelled = await MaterializationRun.objects.filter(
            id__in=orphan_run_ids,
        ).aupdate(state=MaterializationRun.RunState.CANCELLED, completed_at=now)
        total += orphan_cancelled
        for procrastinate_job_id in orphan_job_ids:
            try:
                await current_app.job_manager.cancel_job_by_id_async(
                    procrastinate_job_id, abort=True,
                )
            except Exception:
                logger.warning(
                    "Failed to abort procrastinate job %s", procrastinate_job_id,
                    exc_info=True,
                )

    if total == 0:
        return JsonResponse({"status": "no_active_run", "runs_cancelled": 0})
    return JsonResponse({"status": "cancelled", "runs_cancelled": total})
