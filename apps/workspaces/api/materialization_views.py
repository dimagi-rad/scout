"""Async API views for materialization lifecycle (cancel)."""

import logging
from datetime import UTC, datetime

from django.http import JsonResponse
from procrastinate.contrib.django.procrastinate_app import current_app

from apps.users.decorators import async_login_required
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
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    active = [
        run
        async for run in MaterializationRun.objects.select_related("tenant_schema__tenant").filter(
            tenant_schema__tenant__in=workspace.tenants.all(),
            state__in=list(MaterializationRun.ACTIVE_STATES),
        )
    ]
    if not active:
        return JsonResponse({"status": "no_active_run", "runs_cancelled": 0})

    now = datetime.now(UTC)
    run_ids = [r.id for r in active]
    await MaterializationRun.objects.filter(id__in=run_ids).aupdate(
        state=MaterializationRun.RunState.CANCELLED,
        completed_at=now,
    )

    job_ids = [r.procrastinate_job_id for r in active if r.procrastinate_job_id is not None]
    for job_id in job_ids:
        try:
            await current_app.job_manager.cancel_job_by_id_async(job_id, abort=True)
        except Exception:
            logger.warning("Failed to abort procrastinate job %s", job_id, exc_info=True)

    return JsonResponse(
        {
            "status": "cancelled",
            "runs_cancelled": len(run_ids),
        }
    )
