"""Async API views for materialization lifecycle (cancel, retry)."""

import contextlib
import json
import logging
from datetime import UTC, datetime

from django.http import JsonResponse

from apps.chat.models import Thread, ThreadJob
from apps.users.decorators import async_login_required
from apps.workspaces.api.jobs_cancel import cancel_thread_job
from apps.workspaces.models import MaterializationRun
from apps.workspaces.tasks import materialize_workspace
from apps.workspaces.workspace_resolver import aresolve_workspace
from config.procrastinate import app

logger = logging.getLogger(__name__)


@async_login_required
async def materialization_cancel_view(request, workspace_id):
    """POST /api/workspaces/<workspace_id>/materialization/cancel/

    Marks active MaterializationRuns CANCELLED, then signals procrastinate to
    abort. DB state must flip *before* the abort signal: the worker's
    progress_updater checks run state every page, while abort only fires at the
    next ``await`` (which our to_thread work never reaches).
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    active_runs = [
        r
        async for r in MaterializationRun.objects.select_related("tenant_schema__tenant").filter(
            tenant_schema__tenant__in=workspace.tenants.all(),
            state__in=list(MaterializationRun.ACTIVE_STATES),
        )
    ]
    if not active_runs:
        return JsonResponse({"status": "no_active_run", "runs_cancelled": 0})

    job_ids = {r.procrastinate_job_id for r in active_runs if r.procrastinate_job_id is not None}
    # Scope to thread__user so a member can't cancel another member's chat-driven
    # materialization (which would inject a "cancelled" resume into their thread).
    tjs = [
        tj
        async for tj in ThreadJob.objects.filter(
            procrastinate_job_id__in=job_ids,
            state__in=list(ThreadJob.ACTIVE_STATES),
            thread__user=user,
        )
    ]
    # ALL ThreadJobs (any user) — distinguishes truly-orphan runs (no ThreadJob,
    # e.g. /refresh/ path) from another user's runs, which we must NOT cancel.
    all_tracked_job_ids = {
        pid
        async for pid in ThreadJob.objects.filter(
            procrastinate_job_id__in=job_ids,
        ).values_list("procrastinate_job_id", flat=True)
    }

    total = 0
    tracked_job_ids = set()
    for tj in tjs:
        total += await cancel_thread_job(tj)
        tracked_job_ids.add(tj.procrastinate_job_id)

    # Only truly-orphan runs (no ThreadJob anywhere); other users' runs are skipped.
    orphan_job_ids = job_ids - all_tracked_job_ids
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
                await app.job_manager.cancel_job_by_id_async(
                    procrastinate_job_id,
                    abort=True,
                )
            except Exception:
                logger.warning(
                    "Failed to abort procrastinate job %s",
                    procrastinate_job_id,
                    exc_info=True,
                )

    if total == 0:
        return JsonResponse({"status": "no_active_run", "runs_cancelled": 0})
    return JsonResponse({"status": "cancelled", "runs_cancelled": total})


@async_login_required
async def materialization_retry_view(request, workspace_id):
    """POST /api/workspaces/<workspace_id>/materialize/retry/

    Dispatch a fresh ``materialize_workspace`` job outside the agent. With a
    ``thread_id`` (optional, in the body alongside ``tool_call_id``) it also binds
    a new ThreadJob so the resume mechanism fires when the run finishes.

    Deduplicates against any ACTIVE ThreadJob for the thread before dispatching.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    thread_id = payload.get("thread_id") or ""
    tool_call_id = payload.get("tool_call_id") or ""

    # Validate ownership before binding — else a known thread_id lets an attacker
    # attach a materialization to another user's thread.
    if thread_id:
        thread = await Thread.objects.filter(
            id=thread_id,
            user=user,
            workspace=workspace,
        ).afirst()
        if thread is None:
            return JsonResponse({"error": "thread not found in this workspace"}, status=404)
        # Already in flight: return its identity so the frontend re-binds without
        # double-dispatching (mirrors the MCP run_materialization guard).
        existing = await ThreadJob.objects.filter(
            thread_id=thread_id,
            job_type=ThreadJob.JobType.MATERIALIZATION,
            state__in=list(ThreadJob.ACTIVE_STATES),
        ).afirst()
        if existing is not None:
            return JsonResponse(
                {
                    "status": "already_in_progress",
                    "thread_job_id": str(existing.id),
                }
            )

    try:
        job = await materialize_workspace.defer_async(
            workspace_id=str(workspace.id),
            user_id=str(user.id),
        )
    except Exception:
        logger.exception("materialization_retry_view: failed to dispatch")
        return JsonResponse({"error": "Failed to dispatch materialization"}, status=500)
    job_id = getattr(job, "id", job) if not isinstance(job, int) else job

    if not thread_id:
        return JsonResponse({"status": "started", "procrastinate_job_id": job_id})

    try:
        tj = await ThreadJob.objects.acreate(
            thread_id=thread_id,
            job_type=ThreadJob.JobType.MATERIALIZATION,
            procrastinate_job_id=job_id,
            tool_call_id=tool_call_id,
            state=ThreadJob.State.PENDING,
        )
    except Exception:
        logger.exception("materialization_retry_view: failed to create ThreadJob")
        # Best-effort: cancel the dispatched job so we don't leak background work.
        with contextlib.suppress(Exception):
            await app.job_manager.cancel_job_by_id_async(job_id, abort=True)
        return JsonResponse({"error": "Failed to track retry job"}, status=500)

    return JsonResponse({"status": "started", "thread_job_id": str(tj.id)})
