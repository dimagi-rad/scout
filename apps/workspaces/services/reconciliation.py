"""Settle queue-backed work whose worker never recorded a terminal state.

The worker-side janitors (registered in ``apps.workspaces.tasks``) and the API
pollers (jobs and artifact views) share these decisions, so they live here where
a view can reach them without importing the queue module.

Nothing here may import ``apps.workspaces.tasks``: the resume task is reached by
its registered name instead (see ``tests/test_workspace_task_registry.py``).
"""

import asyncio
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from langchain_core.messages import AIMessage
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.models import ThreadJob
from apps.chat.turn_lease import atry_acquire_turn_lease
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    WorkspaceDataRecovery,
    WorkspaceViewSchema,
)
from apps.workspaces.services.data_operation import to_thread_fresh_db, workspace_data_lock_if_free
from apps.workspaces.services.data_recovery import recovery_query_surface, workspace_recovery_error
from apps.workspaces.services.failure_guidance import compose_failure_summary
from apps.workspaces.services.load_activity import MATERIALIZE_TASK_NAME, REBUILD_VIEW_TASK_NAME
from config.procrastinate import app

logger = logging.getLogger(__name__)

RESUME_TASK_NAME = "apps.workspaces.tasks.resume_thread_after_materialization"

RESUME_STUCK_RUNNING_MESSAGE = (
    "Your materialization completed but the follow-up response was interrupted "
    "(likely a server restart). Please re-ask your question."
)
MATERIALIZATION_FAILED_MESSAGE = (
    "Data loading failed before it could complete. Please retry your request."
)


STALE_JOB_THRESHOLD = timedelta(minutes=10)


def staleness_anchor(tj: ThreadJob):
    """Timestamp from which a ThreadJob's staleness is measured.

    RUNNING jobs measure from the RESUME phase (``started_at``); created_at
    includes the full materialization, so a long materialization + fresh resume
    would otherwise look instantly stale (finding 02#9). Everything else falls
    back to ``created_at`` so an unclaimed job still ages out.
    """
    if tj.state == ThreadJob.State.RUNNING and tj.started_at is not None:
        return tj.started_at
    return tj.created_at


def stale_active_jobs_q(cutoff) -> Q:
    """Predicate matching active ThreadJobs whose staleness anchor is older than
    ``cutoff`` (see :func:`staleness_anchor`).

    Single ORM-side predicate so the janitor never even SELECTs a healthy
    in-flight resume, avoiding the 02#9 false-positive at the source.
    """
    running_stale = Q(
        state=ThreadJob.State.RUNNING,
        started_at__isnull=False,
        started_at__lt=cutoff,
    )
    other_stale = (
        Q(state__in=list(ThreadJob.ACTIVE_STATES))
        & ~Q(state=ThreadJob.State.RUNNING, started_at__isnull=False)
        & Q(created_at__lt=cutoff)
    )
    return running_stale | other_stale


async def _procrastinate_job_status(job_id: int) -> str | None:
    """Return the raw procrastinate job status string, or None when unknown.

    Reads the ``procrastinate_jobs`` table via the ORM model, not
    ``current_app.job_manager``: our import-time ``current_app`` stays bound to the
    unresolved ``FutureApp`` Blueprint (which has no ``job_manager``), so that path
    raised AttributeError on every call. The ORM model sidesteps the app lifecycle.

    Callers must treat ``None`` (exception or unknown id) as "don't touch this row
    this tick" — a sentinel would conflate "not active" with "couldn't tell" and
    let a transient DB blip clean up running jobs.
    """
    try:
        return (
            await ProcrastinateJob.objects.filter(id=job_id)
            .values_list("status", flat=True)
            .afirst()
        )
    except Exception:
        logger.warning(
            "Could not fetch procrastinate status for job %s; skipping reconcile this tick",
            job_id,
            exc_info=True,
        )
        return None


# Explicit status allowlists so an unknown/future procrastinate status can't fall
# into an "act" branch; "aborting" is transitional, so treat it as in-flight (arch #255 10#0).
_PROCRASTINATE_INFLIGHT_STATUSES = frozenset({"todo", "doing", "aborting"})
_PROCRASTINATE_FAILED_STATUSES = frozenset({"failed", "aborted", "cancelled"})
_PROCRASTINATE_SUCCEEDED_STATUS = "succeeded"


async def _resume_in_flight(thread_job_id) -> bool:
    """True when a resume of this ThreadJob is queued or running, or we can't tell."""
    try:
        return await ProcrastinateJob.objects.filter(
            task_name=RESUME_TASK_NAME,
            args__thread_job_id=str(thread_job_id),
            status__in=["todo", "doing"],
        ).aexists()
    except Exception:
        logger.warning(
            "Could not check for a queued resume of %s; skipping reconcile this tick",
            thread_job_id,
            exc_info=True,
        )
        return True


async def reconcile_stale_thread_job(tj: ThreadJob) -> str | None:
    """Reconcile one stale active ThreadJob against its procrastinate job.

    Shared by the worker-side janitor (expire_stale_thread_jobs) and the
    API-side active-jobs poll. The poll backstop exists because the janitor
    runs in the worker process: when the worker itself is sick (June 2026
    incident — its DB connection died and every task, janitor included,
    failed for ~22h) nothing flips stuck ThreadJobs and the frontend spins
    forever. The API process polls anyway, so it can reconcile too.

    Returns the action taken: "failed" (flipped to FAILED), "resumed"
    (resume task deferred), "fallback_failed" (defer raised, flipped FAILED),
    or None (job still active / status unknown / nothing to do).
    """
    status = await _procrastinate_job_status(tj.procrastinate_job_id)
    if status is None:
        return None
    if status in _PROCRASTINATE_INFLIGHT_STATUSES:
        return None
    if tj.state == ThreadJob.State.RUNNING:
        # A resume task claimed it. Measure staleness from the RESUME phase so we
        # only flip a genuinely stuck resume, not a healthy one that just started
        # after a long materialization (finding 02#9).
        anchor = staleness_anchor(tj)
        if anchor is not None and timezone.now() - anchor < STALE_JOB_THRESHOLD:
            return None
        # Worker crashed mid-ainvoke. Mark FAILED directly rather than deferring a
        # duplicate resume that could race with a still-running first invocation.
        updated = await ThreadJob.objects.filter(
            id=tj.id,
            state=ThreadJob.State.RUNNING,
        ).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.RESUME,
            error_summary=(
                "Materialization completed but the follow-up response was "
                "interrupted (likely a server restart). Please retry."
            ),
        )
        if not updated:
            return None
        logger.warning(
            "Reconcile: ThreadJob %s stuck in RUNNING (worker crash?); marked FAILED",
            tj.id,
        )
        await persist_synthetic_failure_message(tj, RESUME_STUCK_RUNNING_MESSAGE)
        return "failed"
    if status in _PROCRASTINATE_FAILED_STATUSES:
        # The task itself failed/was aborted/cancelled — no result for a resume to
        # narrate, so flip straight to FAILED instead of deferring a resume with
        # nothing to say.
        summary = (
            await build_failure_summary_for_job(tj.procrastinate_job_id)
            or MATERIALIZATION_FAILED_MESSAGE
        )
        updated = await ThreadJob.objects.filter(
            id=tj.id,
            state=ThreadJob.State.PENDING,
        ).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.MATERIALIZATION,
            error_summary=summary,
        )
        if not updated:
            return None
        logger.warning(
            "Reconcile: ThreadJob %s pending but procrastinate job %s is %s; marked FAILED",
            tj.id,
            tj.procrastinate_job_id,
            status,
        )
        await persist_synthetic_failure_message(tj, summary)
        return "failed"
    if status != _PROCRASTINATE_SUCCEEDED_STATUS:
        # Unknown/future procrastinate status: never fall into the resume act
        # branch on a status we don't understand — leave the row for the next tick
        # (arch #255, 10#0).
        logger.warning(
            "Reconcile: unrecognized procrastinate status %r for job %s; skipping",
            status,
            tj.procrastinate_job_id,
        )
        return None
    # PENDING job whose materialization SUCCEEDED but was never claimed — safe to
    # defer a fresh resume; the resume task flips the state. A resume already
    # queued or running (e.g. backing off a busy thread) covers it; another would
    # restart the backoff count and churn the queue on every poll.
    if await _resume_in_flight(tj.id):
        return None
    try:
        await app.configure_task(RESUME_TASK_NAME).defer_async(thread_job_id=str(tj.id))
    except Exception:
        logger.exception("Reconcile: failed to defer resume for %s", tj.id)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.RESUME,
            error_summary=(
                "Background queue unavailable; the materialization could "
                "not be resumed. Please retry."
            ),
        )
        return "fallback_failed"
    return "resumed"


async def sweep_stale_thread_jobs() -> dict:
    """Flip ThreadJobs that have been active too long and whose procrastinate
    job is no longer running. Fires the resume task so the user is not stuck
    with a phantom spinner.
    """
    cutoff = timezone.now() - STALE_JOB_THRESHOLD
    flipped = 0
    async for tj in ThreadJob.objects.select_related(
        "thread__workspace",
        "thread__user",
    ).filter(stale_active_jobs_q(cutoff)):
        action = await reconcile_stale_thread_job(tj)
        if action in {"failed", "resumed"}:
            flipped += 1
    return {"flipped": flipped}


# A hard worker death (SIGKILL/OOM/host crash) leaves the procrastinate job 'doing'
# and its MaterializationRun stuck ACTIVE, and the ThreadJob janitor can't see it
# (None for a zombie job; /refresh/ runs have no ThreadJob). Detect it via
# procrastinate's heartbeat stalled-job query and fail the run truthfully (arch #255 03#9).
MATERIALIZATION_STALLED_HEARTBEAT_SECONDS = 300

# Terminal procrastinate statuses for a materialization job: the job is finished,
# so a MaterializationRun still ACTIVE is a zombie the worker never closed out.
_MATERIALIZATION_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "aborted", "cancelled"})


async def _stalled_procrastinate_job_ids() -> set[int]:
    """Best-effort set of procrastinate job ids whose worker heartbeat is stale.

    Wrapped: if the heartbeat query is unavailable (older schema, connector blip)
    we degrade to the job-status signal alone rather than failing the whole tick.
    """
    try:
        stalled = await app.job_manager.get_stalled_jobs(
            seconds_since_heartbeat=MATERIALIZATION_STALLED_HEARTBEAT_SECONDS
        )
    except Exception:
        logger.warning(
            "reconcile_materialization: get_stalled_jobs failed; relying on the "
            "job-status signal only this tick",
            exc_info=True,
        )
        return set()
    return {j.id for j in stalled if j.id is not None}


async def reconcile_workspace_data_recovery(
    recovery: WorkspaceDataRecovery,
    *,
    check_stalled: bool = False,
) -> str | None:
    """Reconcile a recovery row whose worker did not record a terminal state.

    Artifact pages poll from the API process, so this is also a backstop when
    the worker (including its janitor) is unhealthy. A live queue job is never
    disturbed unless Procrastinate reports its worker heartbeat as stalled.
    """
    if recovery.state not in WorkspaceDataRecovery.ACTIVE_STATES:
        return None
    if recovery.procrastinate_job_id is None:
        if timezone.now() - recovery.created_at < STALE_JOB_THRESHOLD:
            return None
        error = "Background recovery was not queued. Please try again."
        updated = await WorkspaceDataRecovery.objects.filter(
            id=recovery.id,
            state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
        ).aupdate(
            state=WorkspaceDataRecovery.State.FAILED,
            error=error,
            completed_at=timezone.now(),
        )
        return "failed" if updated else None

    status = await _procrastinate_job_status(recovery.procrastinate_job_id)
    if status is None:
        return None
    if status in _PROCRASTINATE_INFLIGHT_STATUSES:
        if not check_stalled:
            return None
        stalled_ids = await _stalled_procrastinate_job_ids()
        if recovery.procrastinate_job_id not in stalled_ids:
            return None
        error = "The background recovery worker stopped responding. Please try again."
    elif status == _PROCRASTINATE_SUCCEEDED_STATUS:
        surface = await recovery_query_surface(recovery)
        result = recovery.result or {}
        if (
            surface["status"] == "ready"
            and surface.get("recovery_action") is None
            and not result.get("error")
            and (result.get("cube_schema") or {}).get("ok") is not False
        ):
            updated = await WorkspaceDataRecovery.objects.filter(
                id=recovery.id,
                state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
            ).aupdate(
                state=WorkspaceDataRecovery.State.COMPLETED,
                completed_at=timezone.now(),
                error="",
            )
            return "completed" if updated else None
        error = workspace_recovery_error(recovery.result or {}, surface)
    elif status in _PROCRASTINATE_FAILED_STATUSES:
        error = recovery.error or f"The background recovery job ended ({status}). Please try again."
    else:
        logger.warning(
            "Recovery reconcile: unrecognized procrastinate status %r for job %s; skipping",
            status,
            recovery.procrastinate_job_id,
        )
        return None

    updated = await WorkspaceDataRecovery.objects.filter(
        id=recovery.id,
        state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
    ).aupdate(
        state=WorkspaceDataRecovery.State.FAILED,
        error=error[:1000],
        completed_at=timezone.now(),
    )
    return "failed" if updated else None


async def sweep_stale_workspace_data_recoveries() -> dict:
    """Release artifact recoveries stranded by a stopped background worker."""
    cutoff = timezone.now() - STALE_JOB_THRESHOLD
    reconciled = 0
    async for recovery in WorkspaceDataRecovery.objects.select_related("workspace").filter(
        state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
        created_at__lt=cutoff,
    ):
        action = await reconcile_workspace_data_recovery(recovery, check_stalled=True)
        if action is not None:
            reconciled += 1
    return {"reconciled": reconciled}


async def _fail_thread_jobs_for_dead_materialization(procrastinate_job_id: int) -> None:
    """Fail any active ThreadJob(s) owning a dead materialization job, with a
    truthful summary + synthetic chat message so the UI spinner clears.

    The ThreadJob janitor can't do this for a hard worker death: it returns None
    for a 'doing' zombie job (correct per-tick, permanent for zombies).
    """
    summary = (
        await build_failure_summary_for_job(procrastinate_job_id) or MATERIALIZATION_FAILED_MESSAGE
    )
    async for tj in ThreadJob.objects.select_related("thread__workspace", "thread__user").filter(
        procrastinate_job_id=procrastinate_job_id,
        state__in=list(ThreadJob.ACTIVE_STATES),
    ):
        updated = await ThreadJob.objects.filter(
            id=tj.id,
            state__in=list(ThreadJob.ACTIVE_STATES),
        ).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.MATERIALIZATION,
            error_summary=summary,
        )
        if updated:
            await persist_synthetic_failure_message(tj, summary)


async def fail_zombie_materialization_run(run: MaterializationRun, reason: str) -> bool:
    """CAS-flip an ACTIVE MaterializationRun to FAILED and fail its ThreadJob(s).

    Returns True if this call performed the flip (False if another writer got
    there first). Truthful: records the reason in result so the resume/aggregate
    path narrates a real failure instead of a silent "still loading".
    """
    now = timezone.now()
    result = run.result if isinstance(run.result, dict) else {}
    updated = await MaterializationRun.objects.filter(
        id=run.id,
        state__in=list(MaterializationRun.ACTIVE_STATES),
    ).aupdate(
        state=MaterializationRun.RunState.FAILED,
        completed_at=now,
        result={**result, "error": reason, "reconciled_stale": True},
    )
    if not updated:
        return False
    logger.warning(
        "reconcile_materialization: MaterializationRun %s (tenant_schema %s) flipped to "
        "FAILED — %s",
        run.id,
        run.tenant_schema_id,
        reason,
    )
    if run.procrastinate_job_id is not None:
        await _fail_thread_jobs_for_dead_materialization(run.procrastinate_job_id)
    return True


async def sweep_stale_materialization_runs() -> dict:
    """Fail MaterializationRuns stuck in an ACTIVE state after a hard worker death.

    A run is a zombie when its procrastinate job is stalled (worker heartbeat gone)
    or already terminal while the run row never reached a terminal state. A run
    whose job is still legitimately in flight (live heartbeat, todo/doing) or whose
    status can't be read this tick is left untouched.
    """
    stalled_ids = await _stalled_procrastinate_job_ids()
    failed = 0
    async for run in MaterializationRun.objects.filter(
        state__in=list(MaterializationRun.ACTIVE_STATES),
    ):
        job_id = run.procrastinate_job_id
        if job_id is None:
            continue
        status = await _procrastinate_job_status(job_id)
        if status is None:
            # Transient read failure — can't tell, so don't touch it this tick.
            continue
        is_stalled = job_id in stalled_ids
        is_terminal = status in _MATERIALIZATION_TERMINAL_STATUSES
        if not (is_stalled or is_terminal):
            continue
        reason = (
            "The materialization worker stopped responding before the run finished "
            "(stalled or crashed)."
            if is_stalled
            else f"The materialization job ended ({status}) without recording a terminal run state."
        )
        if await fail_zombie_materialization_run(run, reason):
            failed += 1
    return {"failed": failed, "view_schemas_settled": await _settle_orphaned_view_builds()}


ORPHANED_VIEW_BUILD_ERROR = (
    "The view build stopped before it finished (its worker died or it never started). "
    "Refresh workspace data to rebuild the views."
)


_VIEW_BUILD_TASK_NAMES = (REBUILD_VIEW_TASK_NAME, MATERIALIZE_TASK_NAME)
# Explicit, not derived: only a started job has a worker to heartbeat, so a new
# not-yet-started status must never land here and read as dead.
_STARTED_JOB_STATUSES = ["doing", "aborting"]


def _settle_orphaned_view_build(view_schema_id, stalled_before) -> WorkspaceViewSchema | None:
    """Under W, fail a PROVISIONING view schema that no queued or live job will build.

    A row is PROVISIONING only while a W holder builds it or while the rebuild
    deferred in the same transaction as the flip is still queued. The caller
    holds W, so only queue evidence is left. A job is dead only when it ended or
    its worker stopped heartbeating (Procrastinate's stalled-job rule); any other
    status, including one this code does not know, keeps the row. An active data
    recovery rebuilds the view too, so it also keeps the row. The row lock orders
    this against add/remove_workspace_tenant's flip.
    """
    with transaction.atomic():
        vs = (
            WorkspaceViewSchema.objects.select_for_update()
            .filter(id=view_schema_id, state=SchemaState.PROVISIONING)
            .first()
        )
        if vs is None:
            return None
        # Unlike find_legacy_refresh_jobs, this unindexed full args scan may run
        # under W because it runs only when a row is stranded, normally never.
        live_owner = (
            ProcrastinateJob.objects.filter(
                task_name__in=_VIEW_BUILD_TASK_NAMES,
                args__workspace_id=str(vs.workspace_id),
            )
            .exclude(status__in=list(_MATERIALIZATION_TERMINAL_STATUSES))
            .filter(
                ~Q(status__in=_STARTED_JOB_STATUSES) | Q(worker__last_heartbeat__gte=stalled_before)
            )
            .exists()
        ) or (
            # Stays live only while expire_stale_workspace_data_recoveries
            # keeps settling dead recoveries.
            WorkspaceDataRecovery.objects.filter(
                workspace_id=vs.workspace_id,
                state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
            ).exists()
        )
        if live_owner:
            return None
        vs.state = SchemaState.FAILED
        vs.last_error = ORPHANED_VIEW_BUILD_ERROR
        vs.save(update_fields=["state", "last_error"])
        return vs


async def _settle_orphaned_view_builds() -> int:
    """Fail view schemas stranded in PROVISIONING by a build that will never finish.

    world_state reports a PROVISIONING view as a load in progress, so a dead
    build otherwise tells the user to wait forever. FAILED is honest and any
    later load or refresh of a source rebuilds it.
    """
    stalled_before = timezone.now() - timedelta(seconds=MATERIALIZATION_STALLED_HEARTBEAT_SECONDS)
    candidates = [
        (vs_id, workspace_id)
        async for vs_id, workspace_id in WorkspaceViewSchema.objects.filter(
            state=SchemaState.PROVISIONING
        ).values_list("id", "workspace_id")
    ]
    settled = 0
    for vs_id, workspace_id in candidates:
        try:
            async with workspace_data_lock_if_free(workspace_id) as locked:
                if not locked:
                    continue
                vs = await to_thread_fresh_db(_settle_orphaned_view_build, vs_id, stalled_before)
        except Exception:
            logger.exception("Could not reconcile view schema %s", vs_id)
            continue
        if vs is not None:
            logger.error(
                "Settled orphaned view schema %s (%s) for workspace %s as FAILED: "
                "no live job was building it",
                vs.id,
                vs.schema_name,
                workspace_id,
            )
            settled += 1
    return settled


async def build_failure_summary_for_job(procrastinate_job_id: int) -> str:
    """Read MaterializationRuns for this job and compose a user-facing summary."""
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=procrastinate_job_id,
        )
    ]
    return compose_failure_summary(runs)


async def build_agent_for_resume(workspace, user, conversation_id=None):
    """Build the LangGraph agent for the resume task."""
    mcp_tools = await get_mcp_tools()
    checkpointer = await ensure_checkpointer()
    return await build_agent_graph(
        workspace=workspace,
        user=user,
        checkpointer=checkpointer,
        mcp_tools=mcp_tools,
        conversation_id=conversation_id,
    )


# Bounds how long the synthetic write keeps the thread from the user's chat.
SYNTHETIC_MESSAGE_TIMEOUT_SECONDS = 120


async def persist_synthetic_failure_message(
    thread_job, text: str, *, holds_turn_lease: bool = False
) -> None:
    """Append a plain-text AIMessage to the LangGraph checkpointer for
    ``thread_job.thread`` so the chat UI shows a user-visible explanation when
    the agent never produced one.

    The frontend (apps/chat/thread_views.py:_load_thread_messages) reads
    assistant responses from the checkpointer, so a failure message that
    bypasses this path would never appear. We reuse build_agent_graph because
    aupdate_state requires a compiled graph carrying the AgentState schema and
    the same checkpointer as a normal turn.

    Failures here are logged but never re-raised — the caller has already
    decided this is a terminal failure and a synthetic message is a UX nicety,
    not a correctness invariant. For the same reason it is skipped, not
    waited for, while another run holds the thread's turn lease: the ThreadJob's
    error card still reports the failure.
    """
    try:
        if holds_turn_lease:
            await _append_synthetic_message(thread_job, text)
            return
        lease = await atry_acquire_turn_lease(thread_job.thread_id)
        if lease is None:
            logger.info(
                "resume: thread %s busy; skipped synthetic failure message for tj=%s",
                thread_job.thread_id,
                thread_job.id,
            )
            return
        async with lease.held(), asyncio.timeout(SYNTHETIC_MESSAGE_TIMEOUT_SECONDS):
            await _append_synthetic_message(thread_job, text)
    except Exception:
        logger.warning(
            "resume: failed to persist synthetic failure message for tj=%s",
            thread_job.id,
            exc_info=True,
        )


async def _append_synthetic_message(thread_job, text: str) -> None:
    agent = await build_agent_for_resume(
        thread_job.thread.workspace,
        thread_job.thread.user,
        conversation_id=str(thread_job.thread.id),
    )
    config = {"configurable": {"thread_id": str(thread_job.thread.id)}}
    await agent.aupdate_state(config, {"messages": [AIMessage(content=text)]})
