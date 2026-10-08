"""Continue a chat once a data load ends: the resume, held requests and the flush.

The registered queue tasks in ``apps.workspaces.tasks`` are thin wrappers around
these. Nothing here may import that module (it imports this one), so the two
tasks this re-queues are reached through ``apps.workspaces.task_dispatch``.
"""

import asyncio
import logging
import time

import sentry_sdk
from django.conf import settings
from django.utils import timezone
from langchain_core.messages import HumanMessage
from procrastinate.exceptions import AlreadyEnqueued

from apps.agents.llm_request import LLM_TIMEOUT_ERRORS
from apps.agents.tracing import get_langfuse_callback
from apps.chat import pending_requests, resume_stream
from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.chat.models import PendingRequest, Thread, ThreadJob
from apps.chat.services.agent_execution import (
    append_synthetic_message,
    build_agent_for_resume,
    final_message_content,
    resume_langfuse_span,
)
from apps.chat.tasks import aschedule_thread_title
from apps.chat.turn_lease import TurnLease, atry_acquire_turn_lease
from apps.workspaces.models import (
    VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER,
    SchemaState,
    TenantSchema,
    WorkspaceViewSchema,
)
from apps.workspaces.services.failure_guidance import credential_guidance, summary_failures
from apps.workspaces.services.load_activity import aworkspace_build_pending
from apps.workspaces.services.load_outcome import (
    TENANT_NOT_RUN,
    aggregate_materialization_state,
    build_failure_summary_for_job,
)
from apps.workspaces.services.query_state import semantic_layer_state
from apps.workspaces.task_dispatch import (
    RESUME_THREAD_AFTER_MATERIALIZATION,
    adefer_flush_pending_requests,
    adefer_resume_thread,
)

logger = logging.getLogger(__name__)

RESUME_TASK_NAME = RESUME_THREAD_AFTER_MATERIALIZATION

# User-facing failure copy. The frontend renders these straight from the
# checkpointer (apps/chat/thread_views.py:_load_thread_messages → AIMessage).
RESUME_TIMEOUT_MESSAGE = (
    "The agent took too long to respond after materialization completed. "
    "Please re-ask your question."
)
RESUME_EXCEPTION_MESSAGE = "Sorry, something went wrong while preparing your answer. Please retry."


class _ModelRequestTimeout(TimeoutError):
    """A bounded model request timed out during a resume (LLM_REQUEST_TIMEOUT_S)."""


# A live turn holds the thread for its whole stream, usually under a minute.
# The bound (~17 min) only trips on a thread that stays busy or a lost lease.
RESUME_BUSY_RETRY_BASE_SECONDS = 5
RESUME_BUSY_RETRY_MAX_SECONDS = 60
RESUME_BUSY_MAX_ATTEMPTS = 20
# Beyond AGENT_RESUME_TIMEOUT_S: agent build, MCP tool load and the state reads.
RESUME_SETUP_BUDGET_SECONDS = 120
RESUME_THREAD_BUSY_SUMMARY = (
    "The data load finished, but the conversation stayed busy, so the follow-up "
    "response could not be posted. Please re-ask your question."
)


RESUME_LOST_LEASE_SUMMARY = (
    "The data load finished, but another response took over the conversation before "
    "the follow-up was posted. Please re-ask your question."
)


def _resume_lock(thread_job_id: str) -> str:
    return f"resume-thread-busy-{thread_job_id}"


async def resume_thread_job(thread_job_id: str, busy_attempt: int = 0) -> dict:
    """Body of ``resume_thread_after_materialization``."""
    try:
        tj = await ThreadJob.objects.select_related("thread__workspace", "thread__user").aget(
            id=thread_job_id
        )
    except ThreadJob.DoesNotExist:
        logger.warning("resume: ThreadJob %s not found", thread_job_id)
        return {"status": "missing"}

    if tj.state in ThreadJob.TERMINAL_STATES and tj.state != ThreadJob.State.CANCELLED:
        # Already resumed (idempotent retry); cancellation still gets one resume.
        return {"status": "already_terminal", "state": tj.state}

    lease = await atry_acquire_turn_lease(tj.thread_id)
    if lease is None:
        return await _defer_resume_while_thread_busy(tj, busy_attempt)
    # The ainvoke has its own timeout, but the agent build and state reads around
    # it do not; a resume hung there would heartbeat the lease and block the
    # user's chat until the worker died, so the whole run gets a deadline.
    deadline = asyncio.timeout(settings.AGENT_RESUME_TIMEOUT_S + RESUME_SETUP_BUDGET_SECONDS)
    async with lease.held():
        try:
            async with deadline:
                return await _resume_with_turn_lease(tj, thread_job_id, lease)
        except TimeoutError:
            if not deadline.expired():
                raise
            return await _fail_resume_past_deadline(tj, lease)
        except asyncio.CancelledError:
            # The heartbeat cancels a run that lost the thread; settle the job now
            # rather than leave it RUNNING for the stale sweep to misreport.
            if lease.lost:
                await _fail_resume_lost_lease(tj)
            raise


async def _fail_resume_lost_lease(tj: ThreadJob) -> None:
    logger.error("resume: ThreadJob %s lost its thread's turn lease; marking FAILED", tj.id)
    # No synthetic chat message: another run owns the thread now.
    await ThreadJob.objects.filter(
        id=tj.id, state__in=[ThreadJob.State.RUNNING, ThreadJob.State.PENDING]
    ).aupdate(
        state=ThreadJob.State.FAILED,
        completed_at=timezone.now(),
        failure_phase=ThreadJob.FailurePhase.RESUME,
        error_summary=RESUME_LOST_LEASE_SUMMARY,
    )


async def _fail_resume_past_deadline(tj: ThreadJob, lease: TurnLease) -> dict:
    logger.error("resume: ThreadJob %s overran its deadline; marking FAILED", tj.id)
    updated = await ThreadJob.objects.filter(
        id=tj.id, state__in=[ThreadJob.State.RUNNING, ThreadJob.State.PENDING]
    ).aupdate(
        state=ThreadJob.State.FAILED,
        completed_at=timezone.now(),
        failure_phase=ThreadJob.FailurePhase.RESUME,
        error_summary="The agent took too long to respond after materialization. Please retry.",
    )
    # The write is time-bounded inside persist_synthetic_failure_message, and a
    # lease lost meanwhile cancels this task before it can write.
    if updated and not lease.lost:
        await persist_synthetic_failure_message(tj, RESUME_TIMEOUT_MESSAGE, holds_turn_lease=True)
    return {"status": "resume_deadline"}


async def _defer_resume_while_thread_busy(tj: ThreadJob, busy_attempt: int) -> dict:
    if busy_attempt >= RESUME_BUSY_MAX_ATTEMPTS:
        # No synthetic chat message: writing one now would race the live turn too.
        # A CANCELLED job keeps its state, so the user still sees their Stop.
        gave_up = await ThreadJob.objects.filter(id=tj.id, state=ThreadJob.State.PENDING).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.RESUME,
            error_summary=RESUME_THREAD_BUSY_SUMMARY,
        )
        logger.warning(
            "resume: thread %s stayed busy through %d attempts; ThreadJob %s %s",
            tj.thread_id,
            busy_attempt,
            tj.id,
            "marked FAILED" if gave_up else "already settled",
        )
        return {"status": "thread_busy_gave_up"}
    delay = min(RESUME_BUSY_RETRY_BASE_SECONDS * (2**busy_attempt), RESUME_BUSY_RETRY_MAX_SECONDS)
    # The running job holds no queueing lock once doing, so the re-queue can take
    # it; AlreadyEnqueued means another resume of this job already re-queued.
    try:
        await adefer_resume_thread(
            thread_job_id=str(tj.id),
            busy_attempt=busy_attempt + 1,
            queueing_lock=_resume_lock(str(tj.id)),
            schedule_in={"seconds": delay},
        )
    except AlreadyEnqueued:
        return {"status": "thread_busy_already_queued"}
    logger.info(
        "resume: thread %s busy with another turn; ThreadJob %s retries in %ss (attempt %d)",
        tj.thread_id,
        tj.id,
        delay,
        busy_attempt + 1,
    )
    return {"status": "thread_busy_deferred", "retry_in_seconds": delay}


# Sent as its own marker message: the converter hides marker messages, and the
# request after it must show as the user's own bubble.
HELD_REQUEST_NOTE = "The user's request, written while data loaded, follows; answer it."


# The chat's only request was discarded while its load ran.
NO_REQUEST_NOTE = (
    "The user has no question waiting; tell them briefly that their data is ready to ask about."
)
REQUEST_STILL_WAITING_NOTE = (
    "The user's question is still waiting on the rest of the workspace's data and will be "
    "answered when it loads; tell them briefly that this load finished."
)


async def _thread_has_user_turn(thread_id) -> bool:
    try:
        return await pending_requests.athread_has_user_turn(thread_id)
    except Exception:
        logger.warning("resume: could not read thread %s's checkpoint", thread_id, exc_info=True)
        return True


async def _claim_held_request(tj: ThreadJob, lease: TurnLease):
    try:
        return await pending_requests.aclaim(tj.thread_id, lease.token, thread_job_id=tj.id)
    except Exception:
        # Left waiting, the request shows as unanswered and the user can send it.
        logger.exception("resume: could not claim the held request of thread %s", tj.thread_id)
        return None


async def _resume_with_turn_lease(tj: ThreadJob, thread_job_id: str, lease: TurnLease) -> dict:
    # Excludes RUNNING: aupdate() counts rows MATCHED not changed, so including
    # RUNNING would let a concurrent invocation re-claim a running job and double
    # agent.ainvoke(). CANCELLED is included so the agent can still follow up.
    CLAIMABLE_STATES = [ThreadJob.State.PENDING, ThreadJob.State.CANCELLED]
    # Record started_at so the reconciler measures staleness from the RESUME
    # phase, not created_at (which includes the full materialization). See 02#9.
    resume_started_at = timezone.now()
    claimed = await ThreadJob.objects.filter(
        id=tj.id,
        state__in=CLAIMABLE_STATES,
    ).aupdate(state=ThreadJob.State.RUNNING, started_at=resume_started_at)
    if not claimed:
        logger.info("resume: ThreadJob %s already claimed; no-op", thread_job_id)
        return {"status": "already_claimed"}
    tj.started_at = resume_started_at
    held = await _claim_held_request(tj, lease)
    try:
        return await _resume_claimed_job(tj, thread_job_id, held)
    except BaseException:
        # Settled here too, so a failure before the agent ran leaves the request
        # waiting (offered to send) rather than claimed by a run that is over.
        if held is not None:
            await pending_requests.asettle(held)
        raise


async def _resume_claimed_job(
    tj: ThreadJob, thread_job_id: str, held: pending_requests.ClaimedRequest | None
) -> dict:
    workspace = tj.thread.workspace
    user = tj.thread.user

    # _aggregate_materialization_state is the source of truth for status (not a
    # pre-CAS tj.state snapshot, which mislabelled a Stop-click that raced with
    # completion as "cancelled" when the data had actually loaded).
    status, summary = await aggregate_materialization_state(
        tj.procrastinate_job_id, workspace, str(user.id), tj.materialization_preflight_failures
    )
    recorded_details = []
    for entry in summary:
        if entry.pop("preflight_recorded", False):
            error = entry["error"].strip()
            if error:
                if not error.endswith((".", "!", "?")):
                    error += "."
                recorded_details.append(f"{entry['tenant']} ({entry['provider']}): {error}")
    uncovered_tenants = [
        t.get("display_name", t["tenant"]) for t in summary if t.get("state") == TENANT_NOT_RUN
    ]
    # Named per source *and* per tenant, because a run can carry a dead token on
    # one and revoked access on another — opposite advice, and the agent has to
    # tell them apart to relay either honestly.
    guidance_lines = credential_guidance(summary_failures(summary))
    guidance_text = (
        " Problems the user must act on, named by the source or data source each "
        f"applies to — relay these verbatim: {' '.join(guidance_lines)}"
        if guidance_lines
        else ""
    )

    # Per-tenant runs can all complete while build_view_schema fails, leaving a
    # multi-tenant workspace with NO queryable surface. Detect it so the agent is
    # told the truth (re-running materialization can't fix a build failure).
    view_schema_failed = False
    view_schema_error = ""
    if status in ("completed", "partial"):
        tenant_count = await workspace.workspace_tenants.acount()
        if tenant_count > 1:
            vs = await WorkspaceViewSchema.objects.filter(workspace=workspace).afirst()
            if vs is None or vs.state != SchemaState.ACTIVE:
                view_schema_failed = True
                view_schema_error = (vs.last_error if vs else "") or (
                    "the workspace query layer (view schema) is missing or was never built"
                )

    # The agent answers through the semantic model, so a data load whose Cube
    # schema build failed leaves it with nothing to query even though every run
    # completed — the previous silent path here made the agent claim success
    # and then hit "No active semantic model" with no explanation.
    semantic_state, semantic_error = "ready", ""
    if status in ("completed", "partial") and not view_schema_failed:
        semantic_state, semantic_error = await semantic_layer_state(workspace)
    semantic_unavailable = semantic_state == "unavailable"

    # A completed run is historical evidence, not proof its data still exists.
    # Use current schema state before declaring that another load cannot help.
    missing_active_tenants = []
    if status == "completed" and (view_schema_failed or semantic_unavailable):
        active_ids = {
            tenant_id
            async for tenant_id in TenantSchema.objects.filter(
                tenant__workspace_tenants__workspace=workspace, state=SchemaState.ACTIVE
            ).values_list("tenant_id", flat=True)
        }
        missing_active_tenants = [
            tenant.canonical_name or tenant.external_id
            async for tenant in workspace.tenants.all()
            if tenant.id not in active_ids
        ]
    missing_data_guidance = ""
    if missing_active_tenants:
        missing_data_guidance = (
            f"These sources no longer have active data: {', '.join(missing_active_tenants)}. "
        )
        if VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER in view_schema_error:
            missing_data_guidance += (
                "The data and dependent views expired or were torn down. "
                "Re-running materialization rebuilds them."
            )
        else:
            missing_data_guidance += (
                "Verify current access and account credentials before refreshing their data. "
                "If you cannot access a source, ask someone with access to refresh it."
            )

    if missing_active_tenants:
        body = (
            f"{SYSTEM_RESUME_MARKER} The runs reported completion, but the current data "
            f"and query surface are unavailable. {missing_data_guidance} "
            "Then recheck the workspace query layer and semantic model; do not claim recovery until verified. "
            f"Query layer error: {view_schema_error or semantic_error}. Per-tenant: {summary}"
        )
    elif view_schema_failed:
        if guidance_text or status != "completed":
            body = (
                f"{SYSTEM_RESUME_MARKER} Materialization left incomplete refresh coverage, "
                f"and the workspace query layer (view schema) is unavailable. There is "
                f"currently NO queryable surface for this workspace. Error: {view_schema_error}. "
                f"Do not query or claim that every tenant loaded. Investigate the tenant "
                f"refresh failures below and address any reported account/access problems "
                f"before retrying materialization. If the view still fails after all tenants "
                f"refresh successfully, an administrator must investigate the build error."
                f"{guidance_text} Per-tenant: {summary}"
            )
        elif VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER in view_schema_error:
            # 07#9: FAILED from a cascade teardown, not a build defect — re-running
            # materialization IS the fix, so the advice must invite a re-run.
            body = (
                f"{SYSTEM_RESUME_MARKER} The per-tenant runs reported success, but "
                f"the workspace query layer (the combined view schema that UNION "
                f"ALLs the tenant tables) is currently unavailable because a tenant "
                f"schema it depends on was torn down (inactivity TTL or teardown), "
                f"so the namespaced views were cascade-dropped. There is currently "
                f"NO queryable surface for this workspace. Re-running materialization "
                f"WILL fix this: it rebuilds the tenant data and the view schema. "
                f"Tell the user the data needs to be reloaded and offer to re-run "
                f"materialization. Error: {view_schema_error}. Per-tenant: {summary}"
            )
        else:
            body = (
                f"{SYSTEM_RESUME_MARKER} Per-tenant data loaded successfully, BUT the "
                f"workspace query layer (the combined view schema that UNION ALLs the "
                f"tenant tables) FAILED to build, so there is currently NO queryable "
                f"surface for this workspace. Error: {view_schema_error}. Do NOT re-run "
                f"materialization — it cannot fix this; the per-tenant data is already "
                f"loaded and re-running will hit the same build failure. Tell the user "
                f"plainly that a system-side fix is required and quote the error summary "
                f"above. Per-tenant: {summary}"
            )
    elif semantic_state == "deferred":
        refresh_summary = (
            "This materialization job finished."
            if status == "completed"
            else "Materialization left incomplete refresh coverage; some sources did not refresh."
        )
        body = (
            f"{SYSTEM_RESUME_MARKER} {refresh_summary} Another included source is still "
            f"refreshing. Semantic promotion is deferred: {semantic_error}. "
            "Do not claim a fresh complete semantic snapshot or a build failure. "
            f"Check current data availability before answering.{guidance_text} "
            f"Per-tenant: {summary}"
        )
    elif semantic_unavailable and status != "completed":
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization left incomplete refresh coverage, "
            f"and the semantic model failed to build. Semantic tools "
            f"(list_datasets / semantic_query) will NOT work for this workspace. "
            f"Error: {semantic_error}. Do not query or claim that every tenant loaded. "
            f"Investigate the tenant refresh failures and the semantic build error before "
            f"retrying, addressing any reported account/access problems."
            f"{guidance_text} Per-tenant: {summary}"
        )
    elif semantic_unavailable:
        body = (
            f"{SYSTEM_RESUME_MARKER} The data loaded, BUT the semantic model "
            f"(the Cube schema that makes datasets queryable) FAILED to build, "
            f"so semantic tools (list_datasets / semantic_query) will NOT work "
            f"for this workspace. Error: {semantic_error}. Do NOT silently "
            f"re-run materialization — the data is already loaded and a re-run "
            f"would likely hit the same build error. Tell the user plainly that "
            f"the data loaded but the semantic layer failed to build, and quote "
            f"the error.{guidance_text} Per-tenant: {summary}"
        )
    elif status == "no_runs":
        logger.warning(
            "resume: no MaterializationRun rows for ThreadJob %s job_id=%s; "
            "invoking agent with explanation so the user is not left with a spinner",
            thread_job_id,
            tj.procrastinate_job_id,
        )
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization finished without running any "
            f"pipelines, so NO data was loaded. This means the workspace's tenants "
            f"could not be reached by this user, have no pipeline configured, or "
            f"have no credentials set up. Tell the user what happened and name "
            f"every data source below that was not loaded."
            f"{guidance_text} Per-tenant: {summary}"
        )
    elif status == "partial":
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization completed with PARTIAL data "
            f"(some sources loaded, others failed, were skipped, or did not run). Verify the provenance "
            f"and freshness of available data before using it, and tell the user which sources were "
            f"not refreshed successfully. Older data may still be queryable. Do NOT "
            f"claim that fresh data is loaded for sources marked "
            f"failed, skipped, not_run, or not_published (loaded but never "
            f"published, so not queryable). A source with state=in_progress or state=failed "
            f"and a non-null resume_last_id has partially-loaded rows that the "
            f"next materialization will continue from — do NOT query its table "
            f"as if it were complete.{guidance_text} Per-tenant: {summary}"
        )
    elif status == "failed":
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization FAILED, so this run did not "
            f"produce fresh loaded data. Do NOT claim the materialization completed. "
            f"Summarize every per-source error below, including any top-level error "
            f"field. If older workspace tables are still queryable, do NOT "
            f"present them as results from this failed run; only use them if you "
            f"explicitly verify their provenance and last successful materialization "
            f"time. Suggest checking the workspace connection only when the actual "
            f"error points to authentication or authorization. Do NOT silently re-run "
            f"materialization.{guidance_text} Per-tenant: {summary}"
        )
    elif status == "cancelled":
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization was CANCELLED before it "
            f"finished, so the data load is incomplete or absent. Do NOT claim the "
            f"materialization completed and do NOT query tables as if all data were "
            f"loaded. Tell the user the data load was cancelled and ask whether they "
            f"want to re-run it. Per-tenant: {summary}"
        )
    else:
        if held is not None:
            follow_up = HELD_REQUEST_NOTE
        elif await PendingRequest.objects.filter(
            thread_id=tj.thread_id, thread_job__isnull=True
        ).aexists():
            # Held for the rest of the workspace's data: the flush sends it after.
            follow_up = REQUEST_STILL_WAITING_NOTE
        elif await _thread_has_user_turn(tj.thread_id):
            follow_up = (
                "Please continue with the user's original request using the now-loaded data."
            )
        else:
            follow_up = NO_REQUEST_NOTE
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization just completed "
            f"(status={status}). {follow_up} Per-tenant: {summary}"
        )

    refresh_coverage_user_note = ""
    if uncovered_tenants:
        refresh_coverage_user_note = (
            f"This run did not refresh these data sources: {', '.join(uncovered_tenants)}. "
            "Any of their data in results may be older."
        )
        body += (
            f" IMPORTANT: This run did not refresh these data sources: {', '.join(uncovered_tenants)}. "
            "There is no run record for them; older data may still be included in workspace queries. "
            "Refresh coverage does not establish query coverage. Verify the sources "
            "and last successful refresh times used by any answer, disclose stale or unknown "
            "freshness, and do not claim these sources are excluded without checking."
        )

    # Per-tenant, so a multi-tenant workspace names every affected tenant rather
    # than only the first one that failed.
    test_failure_notes = [
        f"{t['tenant']}: {t['transform_test_failures']}"
        for t in summary
        if t.get("transform_test_failures")
    ]
    if test_failure_notes:
        body += (
            f" Note: the transform models BUILT and their tables are populated, but "
            f"data-quality tests on them FAILED after this load "
            f"({'; '.join(test_failure_notes)}). This is NOT a build failure and NOT "
            f"missing data — do NOT report it as a failed transform, and do NOT "
            f"re-run materialization for it. Tell the user the data loaded and that "
            f"the named data-quality tests failed on the named models, and treat "
            f"figures drawn from those models as unverified."
        )

    if semantic_state == "stale":
        body += (
            f" Note: the semantic model is not up to date after this load "
            f"({semantic_error or 'unknown error'}), so queries run against the "
            f"PREVIOUS semantic model — tables or fields added by this load may "
            f"be missing from list_datasets/semantic_query until a rebuild "
            f"succeeds. Disclose this if it affects your answer."
        )

    if held is not None:
        if HELD_REQUEST_NOTE not in body:
            body += f" {HELD_REQUEST_NOTE}"
        resume_messages = [
            HumanMessage(content=body, id=held.marker_id),
            HumanMessage(content=held.text, id=held.message_id),
        ]
    else:
        resume_messages = [HumanMessage(content=body)]

    timeout_s = settings.AGENT_RESUME_TIMEOUT_S
    sentry_sdk.add_breadcrumb(
        category="resume",
        message="ainvoke_start",
        data={"thread_job_id": str(tj.id), "status": status, "timeout_s": timeout_s},
    )
    logger.info(
        "resume: ainvoke start tj=%s thread=%s workspace=%s status=%s timeout=%ds",
        thread_job_id,
        tj.thread.id,
        workspace.id,
        status,
        timeout_s,
    )
    start = time.monotonic()
    try:
        agent = await build_agent_for_resume(workspace, user, conversation_id=str(tj.thread.id))
        input_state = {
            "messages": resume_messages,
            "workspace_id": str(workspace.id),
            "user_id": str(user.id),
            "thread_id": str(tj.thread.id),
        }
        config = {
            "configurable": {"thread_id": str(tj.thread.id)},
            "recursion_limit": settings.AGENT_RESUME_RECURSION_LIMIT,
        }
        langfuse_handler = get_langfuse_callback(session_id=str(tj.thread.id), user_id=str(user.id))
        if langfuse_handler is not None:
            config["callbacks"] = [langfuse_handler]
        with resume_langfuse_span(
            thread_job_id=thread_job_id,
            thread_id=str(tj.thread.id),
            user_id=str(user.id),
            workspace_id=str(workspace.id),
            status=status,
        ) as langfuse_span:
            # Streamed, so a chat open on the thread shows the answer as it is written.
            try:
                result = await asyncio.wait_for(
                    resume_stream.arun_streamed(agent, input_state, config, tj.thread_id),
                    timeout=timeout_s,
                )
            except LLM_TIMEOUT_ERRORS as exc:
                # Converted only here: the agent build above does its own I/O (the
                # MCP tool list), and a stall there is not a slow answer.
                raise _ModelRequestTimeout("model request timed out") from exc
            if langfuse_span is not None:
                # The resume already succeeded; a tracing error must not mark it agent_failed.
                try:
                    langfuse_span.update(output=final_message_content(result))
                except Exception:
                    logger.warning("resume: failed to record Langfuse output", exc_info=True)
    except TimeoutError as exc:
        elapsed = time.monotonic() - start
        if isinstance(exc, _ModelRequestTimeout):
            # Expected once model requests are bounded, so below Sentry's ERROR level.
            logger.warning(
                "resume: model request timed out (resume elapsed=%.2fs, request limit=%gs, tj=%s)",
                elapsed,
                settings.LLM_REQUEST_TIMEOUT_S,
                thread_job_id,
                exc_info=exc.__cause__,
            )
        else:
            logger.exception(
                "resume: ainvoke timed out after %.2fs (limit=%ds, tj=%s)",
                elapsed,
                timeout_s,
                thread_job_id,
            )
        sentry_sdk.add_breadcrumb(
            category="resume",
            message="ainvoke_timeout",
            data={"thread_job_id": str(tj.id), "elapsed_s": elapsed},
        )
        await persist_synthetic_failure_message(tj, RESUME_TIMEOUT_MESSAGE, holds_turn_lease=True)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.RESUME,
        )
        return {"status": "agent_timeout"}
    except Exception:
        elapsed = time.monotonic() - start
        logger.exception(
            "resume: agent build or invoke failed for thread_job %s after %.2fs",
            thread_job_id,
            elapsed,
        )
        sentry_sdk.add_breadcrumb(
            category="resume",
            message="ainvoke_exception",
            data={"thread_job_id": str(tj.id), "elapsed_s": elapsed},
        )
        await persist_synthetic_failure_message(tj, RESUME_EXCEPTION_MESSAGE, holds_turn_lease=True)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            failure_phase=ThreadJob.FailurePhase.RESUME,
            error_summary=("The agent failed to respond after materialization. Please retry."),
        )
        return {"status": "agent_failed"}
    finally:
        elapsed = time.monotonic() - start
        logger.info(
            "resume: ainvoke complete tj=%s elapsed=%.2fs",
            thread_job_id,
            elapsed,
        )
        if held is not None:
            await pending_requests.asettle(held)
    sentry_sdk.add_breadcrumb(
        category="resume",
        message="ainvoke_complete",
        data={"thread_job_id": str(tj.id), "elapsed_s": time.monotonic() - start},
    )

    # Bump Thread.updated_at so the sidebar's green-dot indicator fires after a
    # background resume. Isolated: a failure here must not contaminate the success
    # path (the agent message was already persisted via ainvoke).
    try:
        await Thread.objects.filter(id=tj.thread_id).aupdate(updated_at=timezone.now())
    except Exception:
        logger.warning(
            "resume: Thread.updated_at bump failed for thread %s; green-dot indicator may not fire",
            tj.thread_id,
            exc_info=True,
        )
    await aschedule_thread_title(tj.thread)

    terminal = (
        ThreadJob.State.CANCELLED
        if status == "cancelled"
        # A view-schema or Cube-schema build failure leaves the workspace with
        # no queryable surface even when every per-tenant run completed, so it
        # is not a success — flip to FAILED so the spinner clears into an
        # error state.
        else (
            ThreadJob.State.FAILED
            if (
                status in ("failed", "partial", "no_runs")
                or view_schema_failed
                or semantic_unavailable
            )
            else ThreadJob.State.COMPLETED
        )
    )
    error_summary = ""
    if terminal == ThreadJob.State.FAILED:
        if missing_active_tenants:
            error_summary = missing_data_guidance
        elif view_schema_failed and (guidance_text or status != "completed"):
            error_summary = (
                "Some tenant data did not refresh successfully, and the workspace query "
                "layer (view schema) "
                f"is unavailable: {view_schema_error}. {' '.join(guidance_lines)}"
            )
        elif view_schema_failed and VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER in view_schema_error:
            # 07#9: cascade teardown — re-running materialization IS the fix.
            error_summary = (
                "The workspace query layer (view schema) is unavailable because a "
                f"tenant schema it depends on was torn down: {view_schema_error}. "
                "Re-running materialization will rebuild it."
            )
        elif view_schema_failed:
            error_summary = (
                "Per-tenant data loaded, but the workspace query layer (view "
                f"schema) failed to build: {view_schema_error}. A system-side "
                "fix is required — re-running materialization will not help."
            )
        elif semantic_unavailable and status != "completed":
            error_summary = (
                "Some tenant data did not refresh successfully, and the semantic model "
                f"failed to build: {semantic_error or 'unknown error'}. Semantic queries "
                f"are unavailable until a rebuild succeeds. {' '.join(guidance_lines)}"
            )
        elif semantic_unavailable:
            error_summary = (
                "Data loaded, but the semantic model failed to build: "
                f"{semantic_error or 'unknown error'}. Semantic queries are "
                "unavailable until a rebuild succeeds."
            )
        elif status == "no_runs":
            error_summary = (
                "Materialization ran no pipelines, so nothing was loaded. "
                "Check the tenant failure details below."
                if recorded_details
                else "Materialization ran no pipelines, so nothing was loaded. "
                "Check that the workspace's tenants are connected to your account "
                "and have credentials configured."
            )
        else:
            error_summary = await build_failure_summary_for_job(tj.procrastinate_job_id)
            if status == "partial" or uncovered_tenants:
                error_summary = f"Materialization did not refresh all data. {error_summary}".strip()
            if not error_summary:
                error_summary = "Materialization did not complete successfully."
            # Run summaries already carry source guidance; no-run tenants have
            # no run row and need their own account remediation added here.
            uncovered_guidance = credential_guidance(
                summary_failures(t for t in summary if t.get("state") == TENANT_NOT_RUN)
            )
            if uncovered_guidance:
                error_summary += " " + " ".join(uncovered_guidance)
    if error_summary:
        no_runs_guidance = guidance_lines if status == "no_runs" else []
        error_summary = " ".join(
            part.strip()
            for part in [
                error_summary,
                *recorded_details,
                *no_runs_guidance,
                refresh_coverage_user_note,
            ]
            if part.strip()
        )
    # CAS-scoped to state=RUNNING: a concurrent cancel during ainvoke leaves the
    # row CANCELLED, so this matches zero rows rather than clobbering it back to a
    # success terminal; we then re-read the actual persisted state below.
    failure_phase = ""
    if terminal == ThreadJob.State.FAILED:
        query_build_failed = (
            status == "completed"
            and not missing_active_tenants
            and view_schema_failed
            and VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER not in view_schema_error
        )
        failure_phase = (
            ThreadJob.FailurePhase.QUERY_BUILD
            if query_build_failed
            else ThreadJob.FailurePhase.MATERIALIZATION
        )
    updated = await ThreadJob.objects.filter(
        id=tj.id,
        state=ThreadJob.State.RUNNING,
    ).aupdate(
        state=terminal,
        completed_at=timezone.now(),
        error_summary=error_summary,
        failure_phase=failure_phase,
    )
    if not updated:
        actual_state = (
            await ThreadJob.objects.filter(id=tj.id)
            .values_list(
                "state",
                flat=True,
            )
            .afirst()
        )
        logger.info(
            "resume: ThreadJob %s state changed during ainvoke; not clobbering "
            "(intended terminal=%s, actual=%s)",
            thread_job_id,
            terminal,
            actual_state,
        )
        return {"status": "resumed", "terminal_state": actual_state or terminal}
    return {"status": "resumed", "terminal_state": terminal}


# Sent ahead of a request the workspace flush sends: one held for a workspace
# load (another member's, or one a read-only member could not start).
FLUSH_NOTE = (
    f"{SYSTEM_RESUME_MARKER} A workspace data load ended while this message waited; "
    "answer it from the data now available, and say so if what it needs did not load."
)
FLUSH_FAILED_MESSAGE = "I couldn't finish answering this after your data loaded. Please ask again."
# Lets the load that queued the flush finish, so the flush does not see it pending.
PENDING_FLUSH_DELAY_SECONDS = 5


# A flush that finds a build still running (often one the ending load queued) looks again.
PENDING_FLUSH_RECHECK_SECONDS = 15
# Requests sent per flush run; the rest go in a run queued after it, so one run
# never holds a worker for each request's full answer in turn.
FLUSH_BATCH = 3


async def defer_pending_flush(workspace_id, delay: int = PENDING_FLUSH_DELAY_SECONDS) -> None:
    """Queue a flush of the workspace's held requests; never raises (the sweep is the backstop)."""
    if not workspace_id:
        return
    try:
        await adefer_flush_pending_requests(
            workspace_id=str(workspace_id),
            queueing_lock=f"pending-flush:{workspace_id}",
            schedule_in={"seconds": delay},
        )
    except AlreadyEnqueued:
        pass
    except Exception:
        logger.exception("Could not queue the held-request flush for workspace %s", workspace_id)


async def flush_workspace_requests(workspace_id: str) -> dict:
    """Send the workspace's held requests that no load of their own will send.

    Runs when a load of the workspace ends, and from the minute sweep. A load
    still under way flushes them itself when it ends.
    """
    sent = 0
    thread_ids = await pending_requests.aflushable_thread_ids(workspace_id)
    for thread_id in thread_ids[:FLUSH_BATCH]:
        # Per request: answering one takes minutes, and a load may start meanwhile.
        if await aworkspace_build_pending(workspace_id):
            await defer_pending_flush(workspace_id, PENDING_FLUSH_RECHECK_SECONDS)
            return {"status": "load_pending", "sent": sent}
        try:
            sent += await _flush_thread(thread_id)
        except Exception:
            logger.exception("flush: could not send the held request of thread %s", thread_id)
    if len(thread_ids) > FLUSH_BATCH:
        await defer_pending_flush(workspace_id)
    return {"status": "flushed", "sent": sent}


async def sweep_pending_requests() -> dict:
    """Backstop for a flush that never ran: a lost defer, or a hold racing a load's end.

    Queues a flush per workspace rather than running them, so one slow answer
    never holds a worker across workspaces; the queueing lock dedupes them.
    """
    workspace_ids = await pending_requests.aflushable_workspace_ids()
    for workspace_id in workspace_ids:
        await defer_pending_flush(workspace_id)
    return {"queued": len(workspace_ids)}


async def _flush_thread(thread_id) -> int:
    thread = await Thread.objects.select_related("workspace", "user").aget(id=thread_id)
    lease = await atry_acquire_turn_lease(thread_id)
    if lease is None:
        # Answering now: that turn takes the request with it.
        return 0
    answered = False
    try:
        async with lease.held():
            if not await pending_requests.acount_flush_attempt(thread_id):
                return 0
            held = await pending_requests.aclaim(thread_id, lease.token)
            if held is None:
                return 0
            try:
                # The whole turn, setup included: the user's chat is busy until it ends.
                async with asyncio.timeout(
                    settings.AGENT_RESUME_TIMEOUT_S + RESUME_SETUP_BUDGET_SECONDS
                ):
                    answered = await _answer_flushed_request(thread, held)
            except TimeoutError:
                logger.exception("flush: the held request of thread %s timed out", thread_id)
            finally:
                await pending_requests.asettle(held)
            # The run saves the user's message before the model answers, so a run
            # that failed after that leaves the message sent, with no reply.
            try:
                async with asyncio.timeout(pending_requests.SETTLE_TIMEOUT_SECONDS):
                    landed = await pending_requests.athread_has_message(thread_id, held.message_id)
            except Exception:
                # Unknown: say nothing rather than claim a reply failed.
                logger.warning("flush: could not read thread %s", thread_id, exc_info=True)
                landed = False
            if landed and not answered:
                await persist_synthetic_thread_message(thread, FLUSH_FAILED_MESSAGE)
            answered = answered or landed
    except asyncio.CancelledError:
        # The heartbeat cancels the task when another run takes the thread; that
        # ends this request (settled above), not the flush of the others.
        if not lease.lost:
            raise
        asyncio.current_task().uncancel()
        return 0
    if not answered:
        return 0
    try:
        await Thread.objects.filter(id=thread_id).aupdate(updated_at=timezone.now())
    except Exception:
        logger.warning("flush: Thread.updated_at bump failed for %s", thread_id, exc_info=True)
    await aschedule_thread_title(thread)
    return 1


async def _answer_flushed_request(thread: Thread, held) -> bool:
    workspace, user = thread.workspace, thread.user
    try:
        agent = await build_agent_for_resume(workspace, user, conversation_id=str(thread.id))
        config = {
            "configurable": {"thread_id": str(thread.id)},
            "recursion_limit": settings.AGENT_RESUME_RECURSION_LIMIT,
        }
        langfuse_handler = get_langfuse_callback(session_id=str(thread.id), user_id=str(user.id))
        if langfuse_handler is not None:
            config["callbacks"] = [langfuse_handler]
        try:
            await asyncio.wait_for(
                resume_stream.arun_streamed(
                    agent,
                    {
                        "messages": [
                            HumanMessage(content=FLUSH_NOTE, id=held.marker_id),
                            HumanMessage(content=held.text, id=held.message_id),
                        ],
                        "workspace_id": str(workspace.id),
                        "user_id": str(user.id),
                        "thread_id": str(thread.id),
                    },
                    config,
                    thread.id,
                ),
                timeout=settings.AGENT_RESUME_TIMEOUT_S,
            )
        except LLM_TIMEOUT_ERRORS as exc:
            raise _ModelRequestTimeout("model request timed out") from exc
    except _ModelRequestTimeout as exc:
        logger.warning(
            "flush: model request timed out for the held request of thread %s",
            thread.id,
            exc_info=exc.__cause__,
        )
        return False
    except Exception:
        # Settled after: unsent, it waits again (its one flush spent) for the user to send.
        logger.exception("flush: agent failed for the held request of thread %s", thread.id)
        return False
    return True


# Bounds how long the synthetic write keeps the thread from the user's chat.
SYNTHETIC_MESSAGE_TIMEOUT_SECONDS = 120


async def persist_synthetic_failure_message(
    thread_job, text: str, *, holds_turn_lease: bool = False
) -> None:
    """Append a plain-text AIMessage to the LangGraph checkpointer for
    ``thread_job.thread`` so the chat UI shows a user-visible explanation when
    the agent never produced one. Tool calls the turn left open are answered
    as interrupted first.

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
            async with asyncio.timeout(SYNTHETIC_MESSAGE_TIMEOUT_SECONDS):
                await append_synthetic_message(thread_job.thread, text)
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
            await append_synthetic_message(thread_job.thread, text)
    except Exception:
        logger.warning(
            "resume: failed to persist synthetic failure message for tj=%s",
            thread_job.id,
            exc_info=True,
        )


async def persist_synthetic_thread_message(thread, text: str) -> None:
    """``persist_synthetic_failure_message`` for a caller holding ``thread``'s turn
    lease with no ThreadJob, such as the held-request flush. Never raises."""
    try:
        async with asyncio.timeout(SYNTHETIC_MESSAGE_TIMEOUT_SECONDS):
            await append_synthetic_message(thread, text)
    except Exception:
        logger.warning(
            "Could not persist a synthetic message on thread %s", thread.id, exc_info=True
        )
