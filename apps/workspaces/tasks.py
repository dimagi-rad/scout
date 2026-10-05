"""Background tasks for schema lifecycle management."""

import logging

from django.utils import timezone

from apps.chat import resume_stream
from apps.chat.services import continuation
from apps.chat.services.continuation import defer_pending_flush as _defer_pending_flush
from apps.common.capacity import classify_capacity_error
from apps.users.models import User
from apps.workspaces.access import (
    NO_SOURCES,
    NO_SOURCES_MESSAGE,
    TENANT_ACCESS_LOST,
    WorkspaceAccess,
    access_denied_body,
    aresolve_workspace_access_ex,
)
from apps.workspaces.models import (
    SchemaState,
    WorkspaceDataRecovery,
    WorkspaceRole,
    WorkspaceViewSchema,
)
from apps.workspaces.services import materialize, publication, refresh, retirement
from apps.workspaces.services.access_freshness import (
    FRESHNESS_ERROR_CODES,
    VerificationBudget,
)
from apps.workspaces.services.data_operation import (
    to_thread_fresh_db as _to_thread_fresh_db,
)
from apps.workspaces.services.data_operation import (
    workspace_data_lock,
)
from apps.workspaces.services.data_recovery import (
    CAPACITY_REFUSED_KEY,
    CHAT_RECOVERY_SOURCE,
    recovery_query_surface,
)
from apps.workspaces.services.data_recovery import (
    ROLE_DENIED_MESSAGE as _ROLE_DENIED_MESSAGE,
)
from apps.workspaces.services.data_recovery import (
    workspace_recovery_error as _workspace_recovery_error,
)
from apps.workspaces.services.load_generations import (
    INTENT_FULL_REFRESH,
    INTENT_RECONCILE_MISSING,
)
from apps.workspaces.services.query_state import (
    included_tenant_snapshot_state as _included_tenant_snapshot_state,
)
from apps.workspaces.services.reconciliation import (
    sweep_stale_materialization_runs,
    sweep_stale_thread_jobs,
    sweep_stale_workspace_data_recoveries,
)
from apps.workspaces.services.schema_manager import (
    SchemaManager,
)
from apps.workspaces.task_dispatch import JOB_RETENTION_HOURS, register_inline_materializer
from config.procrastinate import app

logger = logging.getLogger(__name__)


@app.task(pass_context=True)
async def refresh_tenant_schema(
    context,
    schema_id: str,
    membership_id: str,
    actor_user_id: str = "",
    workspace_id: str = "",
) -> dict:
    """See ``refresh.refresh_tenant_schema``; a held request waiting on it is flushed after."""
    try:
        return await refresh.refresh_tenant_schema(
            context,
            schema_id,
            membership_id,
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
        )
    finally:
        await _defer_pending_flush(workspace_id)


register_inline_materializer(materialize.materialize_workspace_blocking)


@app.task(pass_context=True)
async def materialize_workspace(
    context,
    workspace_id: str,
    user_id: str = "",
    load_intent: dict | None = None,
    only_unserved: bool = False,
    notify_thread: bool = True,
) -> dict:
    """Load a workspace, then always resume its waiting chat thread (if any)."""
    return await materialize.materialize_workspace(
        context,
        workspace_id,
        user_id,
        load_intent=load_intent,
        only_unserved=only_unserved,
        notify_thread=notify_thread,
    )


async def _recovery_intent(workspace) -> str:
    """What a data-restore repair must ask of the tenants' loads.

    Missing data is reconciled: whatever is already published satisfies it. But
    the same repair is offered when data is present and its latest load did not
    complete, and reconciling would reuse that same published generation and
    change nothing, so that case asks for a fresh load.
    """
    view = await WorkspaceViewSchema.objects.filter(
        workspace=workspace, state=SchemaState.ACTIVE
    ).afirst()
    coverage = view.tenant_coverage if view is not None else None
    if await _included_tenant_snapshot_state(workspace, coverage) == "unsafe":
        return INTENT_FULL_REFRESH
    return INTENT_RECONCILE_MISSING


@app.task
async def drop_abandoned_candidate(
    schema_id: str,
    attempt: int = 0,
    last_attempt_at: str = "",
    load_job_id: int | None = None,
    busy_count: int = 0,
) -> None:
    """Drop the partial data of a failed candidate no load will resume."""
    await retirement.drop_abandoned_candidate(
        schema_id,
        attempt=attempt,
        last_attempt_at=last_attempt_at,
        load_job_id=load_job_id,
        busy_count=busy_count,
    )


@app.periodic(cron="7,22,37,52 * * * *")
@app.task
async def sweep_workspace_load_candidates(timestamp: int = 0) -> dict:
    """Reclaim workspace-load candidates whose writer died or no load will resume."""
    return await retirement.sweep_workspace_load_candidates()


@app.task
async def drop_failed_refresh_schema(schema_id: str) -> None:
    """Drop the physical schema of a refresh candidate settled as FAILED."""
    await retirement.drop_failed_refresh_schema(schema_id)


@app.periodic(cron="*/15 * * * *")
@app.task
async def reconcile_refresh_candidates(timestamp: int = 0) -> dict:
    """Settle refresh candidates whose queue job finished or whose worker died."""
    return await retirement.reconcile_refresh_candidates()


@app.periodic(cron="*/30 * * * *")
@app.task
async def expire_inactive_schemas(timestamp: int = 0) -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    `timestamp` is supplied by the procrastinate periodic deferrer; the default
    lets tests invoke this task directly.
    """
    await retirement.expire_inactive_schemas()


@app.task
async def rebuild_workspace_view_schema(workspace_id: str, revive_retired: bool = False) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace."""
    return await publication.rebuild_workspace_view_schema(
        workspace_id, revive_retired=revive_retired
    )


@app.task
async def rebuild_workspace_semantic_model(workspace_id: str) -> dict:
    """Rebuild the semantic model + Cube schema after workspace data changed shape."""
    return await publication.rebuild_workspace_semantic_model_core(workspace_id)


@app.task(pass_context=True)
async def recover_workspace_data(context, recovery_id: str) -> dict:
    """See ``_recover_workspace_data``; a held request waiting on it is flushed after."""
    try:
        return await _recover_workspace_data(context, recovery_id)
    finally:
        await _defer_flush_after_recovery(recovery_id)


async def _defer_flush_after_recovery(recovery_id: str) -> None:
    try:
        workspace_id = (
            await WorkspaceDataRecovery.objects.filter(id=recovery_id)
            .values_list("workspace_id", flat=True)
            .afirst()
        )
    except Exception:
        # From the task's finally: never replace its outcome; the sweep is the backstop.
        logger.exception("Could not find the workspace of recovery %s", recovery_id)
        return
    await _defer_pending_flush(workspace_id)


async def _recover_workspace_data(context, recovery_id: str) -> dict:
    """Repair the least healthy layer of a workspace's artifact query surface.

    The requested recovery type captures why the job was created. The task
    reassesses after waiting for any in-flight materialization, because another
    session may have repaired the physical layer while this job was queued. It
    then runs only the remaining repair and records a durable terminal state.
    """
    try:
        recovery = await WorkspaceDataRecovery.objects.select_related("workspace").aget(
            id=recovery_id
        )
    except WorkspaceDataRecovery.DoesNotExist:
        logger.warning("recover_workspace_data: recovery %s not found", recovery_id)
        return {"status": "missing"}

    now = timezone.now()
    claimed = await WorkspaceDataRecovery.objects.filter(
        id=recovery.id,
        state=WorkspaceDataRecovery.State.PENDING,
    ).aupdate(
        state=WorkspaceDataRecovery.State.RUNNING,
        procrastinate_job_id=context.job.id,
        started_at=now,
        error="",
    )
    if not claimed:
        return {"status": recovery.state}

    if recovery.requested_by_id is None:
        error = "The user who requested recovery no longer exists. Ask a workspace member to retry."
        await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
            state=WorkspaceDataRecovery.State.FAILED,
            error=error,
            completed_at=timezone.now(),
        )
        return {"status": "failed", "error": error}

    result: dict = {}
    try:
        async with workspace_data_lock(str(recovery.workspace_id)):
            await materialize.await_in_progress_materializations(str(recovery.workspace_id))

            requester = await User.objects.filter(id=recovery.requested_by_id).afirst()
            access = (
                await aresolve_workspace_access_ex(
                    requester,
                    recovery.workspace_id,
                    minimum_role=WorkspaceRole.READ_WRITE,
                    verification=VerificationBudget.BACKGROUND,
                )
                if requester is not None
                else None
            )
            if access is None or not access.granted:
                raise ValueError(_recovery_requester_denied_message(access))

            try:
                reconciliation = await _to_thread_fresh_db(
                    SchemaManager().reconcile_view_publication, recovery.workspace
                )
                if reconciliation.get("status") == "republish_failed":
                    logger.warning(
                        "Recovery %s: view publication diverges and could not be republished: %s",
                        recovery_id,
                        reconciliation.get("error"),
                    )
            except Exception:
                # A republish can fail for the very reason this recovery exists
                # (e.g. no loaded source yet); the repair below must still run.
                logger.exception(
                    "Reconciling the view publication for recovery %s failed", recovery_id
                )
            surface = await recovery_query_surface(recovery)
            action = surface.get("recovery_action")
            if (
                recovery.source_type == CHAT_RECOVERY_SOURCE
                and action == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION
            ):
                # Nobody approved a reload: a chat only asks for the data model (#714).
                result = {"error": _CHAT_RECOVERY_NEEDS_RELOAD}
            elif action == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION:
                result = await materialize.materialize_workspace_core(
                    str(recovery.workspace_id),
                    str(recovery.requested_by_id),
                    context.job.id,
                    intent_kind=await _recovery_intent(recovery.workspace),
                )
            elif action == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD:
                result = await publication.rebuild_workspace_semantic_model_core(
                    str(recovery.workspace_id)
                )
            elif action == WorkspaceDataRecovery.RecoveryType.VIEW_REBUILD:
                result = await publication.rebuild_workspace_view_schema(
                    str(recovery.workspace_id), revive_retired=True
                )
            elif (
                recovery.recovery_type == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD
                and surface["status"] == "ready"
                and surface.get("semantic_status") == "stale"
            ):
                # The catalog still serves but its latest build failed; chat asks for this (#714).
                result = await publication.rebuild_workspace_semantic_model_core(
                    str(recovery.workspace_id)
                )
            elif surface["status"] == "ready":
                result = {"status": "already_recovered"}
            else:
                result = {"error": surface["message"]}

            final_surface = await recovery_query_surface(recovery)
            # A previously promoted Cube schema can remain readable after a
            # failed rebuild. Serving that fallback is safe, but it must not
            # turn an unsuccessful recovery attempt into a reported success.
            cube_result = result.get("cube_schema") or {}
            if cube_result.get(CAPACITY_REFUSED_KEY):
                result = {**result, CAPACITY_REFUSED_KEY: True}
            if (
                final_surface["status"] != "ready"
                or final_surface.get("recovery_action") is not None
                or result.get("error")
                or cube_result.get("ok") is False
            ):
                error = _workspace_recovery_error(result, final_surface)
                await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
                    state=WorkspaceDataRecovery.State.FAILED,
                    result=result,
                    error=error,
                    completed_at=timezone.now(),
                )
                return {"status": "failed", "error": error, "result": result}

            await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
                state=WorkspaceDataRecovery.State.COMPLETED,
                result=result,
                error="",
                completed_at=timezone.now(),
            )
            return {"status": "completed", "result": result}
    except Exception as exc:
        logger.exception("Workspace data recovery %s failed", recovery.id)
        if classify_capacity_error(exc) is not None:
            result = {**result, CAPACITY_REFUSED_KEY: True}
        error = str(exc)[:1000] or "Scout could not restore this artifact's data."
        await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
            state=WorkspaceDataRecovery.State.FAILED,
            result=result,
            error=error,
            completed_at=timezone.now(),
        )
        return {"status": "failed", "error": error, "result": result}


_CHAT_RECOVERY_NEEDS_RELOAD = (
    "The data model can't be rebuilt from the loaded data: it needs a data refresh first."
)


def _recovery_requester_denied_message(access: WorkspaceAccess | None) -> str:
    if access is not None and access.denied_reason == TENANT_ACCESS_LOST:
        projects = ", ".join(access.lost_tenant_names) or "this workspace's data sources"
        # Both causes stay named: a lost membership can't tell a disconnect from
        # upstream removal, and a different member may be reading this card.
        return (
            f"The requesting user no longer has access to: {projects} through a connected "
            "account. If they disconnected it, they should reconnect it in Settings → "
            "Connections; if their access was removed in the provider, an admin there "
            "must restore it."
        )
    if access is not None and access.denied_reason == NO_SOURCES:
        return NO_SOURCES_MESSAGE
    if access is not None and access.denied_reason in FRESHNESS_ERROR_CODES:
        return f"The requesting user's access could not be confirmed: {access_denied_body(access)['error']}"
    return _ROLE_DENIED_MESSAGE


@app.task
async def teardown_view_schema_task(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    await retirement.teardown_view_schema(view_schema_id)


@app.task
async def teardown_schema(schema_id: str, attempt: int = 0) -> None:
    """Retire a tenant schema once nothing outside it depends on it, then mark EXPIRED."""
    await retirement.teardown_schema(schema_id, attempt)


@app.periodic(cron="*/15 * * * *")
@app.task
async def expire_stale_thread_jobs(timestamp: int = 0) -> dict:
    """Flip ThreadJobs that have been active too long and whose procrastinate
    job is no longer running. Fires the resume task so the user is not stuck
    with a phantom spinner.
    """
    return await sweep_stale_thread_jobs()


@app.periodic(cron="*/15 * * * *")
@app.task
async def expire_stale_workspace_data_recoveries(timestamp: int = 0) -> dict:
    """Release artifact recoveries stranded by a stopped background worker."""
    return await sweep_stale_workspace_data_recoveries()


@app.periodic(cron="*/15 * * * *")
@app.task
async def reconcile_stale_materialization_runs(timestamp: int = 0) -> dict:
    """Fail MaterializationRuns stuck ACTIVE after a hard worker death, then settle
    view schemas whose build will never finish."""
    return await sweep_stale_materialization_runs()


@app.periodic(cron="17 3 * * *")
@app.task
async def prune_old_procrastinate_jobs(timestamp: int = 0) -> dict:
    """Delete old finalized procrastinate jobs (and their events) so the queue
    tables don't grow without bound.

    Only 'succeeded' jobs are pruned (delete_old_jobs' default — failed/cancelled/
    aborted are retained): the reconciler treats an unknown job id as "can't tell,
    don't touch", so pruning a job still referenced by an active ThreadJob/
    MaterializationRun would strand it. The 7-day horizon is far longer than the
    15-minute stale-job janitor's window, so any active row referencing a succeeded
    job has long since been reconciled before its job becomes prunable (arch #255,
    10#0, reconciler↔retention coupling).
    """
    try:
        await app.job_manager.delete_old_jobs(nb_hours=JOB_RETENTION_HOURS)
    except Exception:
        logger.warning("prune_old_procrastinate_jobs: delete_old_jobs failed", exc_info=True)
        return {"pruned": False}
    logger.info(
        "prune_old_procrastinate_jobs: pruned succeeded jobs older than %sh", JOB_RETENTION_HOURS
    )
    return {"pruned": True}


# The scout-worker-silent alarm (infra/scout-stack.yml) fires on 15 minutes without a
# worker log line, and an idle worker's janitors log nothing, so it flapped all day.
# The lock stops ticks piling up behind a long job: one queued tick is enough.
@app.periodic(cron="*/5 * * * *")
@app.task(queueing_lock="log_worker_keepalive")
async def log_worker_keepalive(timestamp: int = 0) -> None:
    """Log one line, so a worker that is running its jobs is never silent."""
    logger.info("worker keepalive: periodic jobs are running")


@app.task(pass_context=True)
async def resume_thread_after_materialization(
    context, thread_job_id: str, busy_attempt: int = 0
) -> dict:
    """Inject a system-framed message into the LangGraph conversation and
    re-invoke the agent so it can respond to the original request with the
    now-loaded data.

    Runs only while holding the thread's turn lease, so it never writes the
    checkpoint while a live chat turn is streaming on the same thread; a busy
    thread re-queues it with backoff (arch review R08).
    """
    return await continuation.resume_thread_job(thread_job_id, busy_attempt)


@app.task
async def flush_pending_requests(workspace_id: str) -> dict:
    """Send the workspace's held requests that no load of their own will send.

    Runs when a load of the workspace ends, and from the minute sweep. A load
    still under way flushes them itself when it ends.
    """
    return await continuation.flush_workspace_requests(workspace_id)


@app.periodic(cron="* * * * *")
@app.task
async def sweep_pending_requests(timestamp: int = 0) -> dict:
    """Backstop for a flush that never ran: a lost defer, or a hold racing a load's end.

    Queues a flush per workspace rather than running them, so one slow answer
    never holds a worker across workspaces; the queueing lock dedupes them.
    """
    return await continuation.sweep_pending_requests()


@app.periodic(cron="*/15 * * * *")
@app.task
async def prune_resume_streams(timestamp: int = 0) -> dict:
    """Drop streamed resume text old enough that no chat still tails it."""
    return {"deleted": await resume_stream.aprune()}
