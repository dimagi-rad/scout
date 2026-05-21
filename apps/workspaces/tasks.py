"""Background tasks for schema lifecycle management."""

import asyncio
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from langchain_core.messages import HumanMessage
from procrastinate.contrib.django.procrastinate_app import current_app

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.chat.models import Thread, ThreadJob
from apps.users.models import TenantMembership
from apps.users.services.credential_resolver import aresolve_credential, resolve_credential
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager
from config.procrastinate import app
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import (
    MaterializationCancelled,
    run_pipeline,
)

logger = logging.getLogger(__name__)


@app.task
async def refresh_tenant_schema(schema_id: str, membership_id: str) -> dict:
    """Provision a new schema and run the materialization pipeline.

    On success: marks state=ACTIVE, schedules teardown of old active schemas.
    On failure: drops the new schema, marks state=FAILED.
    """
    try:
        new_schema = await TenantSchema.objects.select_related("tenant").aget(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.exception("refresh_tenant_schema: schema %s not found", schema_id)
        return {"error": "Schema not found"}

    try:
        membership = await TenantMembership.objects.select_related("tenant", "user").aget(
            id=membership_id
        )
    except TenantMembership.DoesNotExist:
        new_schema.state = SchemaState.FAILED
        await new_schema.asave(update_fields=["state"])
        return {"error": "Membership not found"}

    # Step 1: Create the physical schema in the managed database
    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.create_physical_schema, new_schema)
    except Exception:
        logger.exception("Failed to create schema '%s'", new_schema.schema_name)
        new_schema.state = SchemaState.FAILED
        await new_schema.asave(update_fields=["state"])
        return {"error": "Failed to create schema"}

    # Step 2: Resolve credential and run materialization pipeline
    credential = resolve_credential(membership)
    if credential is None:
        await _drop_schema_and_fail(new_schema)
        return {"error": "No credential available"}

    try:
        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}
        pipeline_name = provider_pipeline_map.get(membership.tenant.provider)
        if pipeline_name is None:
            await _drop_schema_and_fail(new_schema)
            return {"error": f"No pipeline configured for provider '{membership.tenant.provider}'"}
        pipeline_config = registry.get(pipeline_name)
        await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        await _drop_schema_and_fail(new_schema)
        return {"error": "Materialization failed"}

    # Step 3: Mark new schema as active
    new_schema.state = SchemaState.ACTIVE
    await new_schema.asave(update_fields=["state"])

    # Step 4: Schedule teardown of previously active schemas with a delay to allow
    # in-flight queries against the old schema to complete before it is dropped.
    old_schemas = TenantSchema.objects.filter(
        tenant=new_schema.tenant,
        state=SchemaState.ACTIVE,
    ).exclude(id=new_schema.id)
    async for old_schema in old_schemas:
        old_schema.state = SchemaState.TEARDOWN
        await old_schema.asave(update_fields=["state"])
        await teardown_schema.configure(
            schedule_in={"seconds": int(timedelta(minutes=30).total_seconds())},
        ).defer_async(schema_id=str(old_schema.id))

    logger.info("Refresh complete: schema '%s' is now active", new_schema.schema_name)
    return {"status": "active", "schema_id": schema_id}


@app.task(pass_context=True)
async def materialize_workspace(
    context,
    workspace_id: str,
    user_id: str = "",
) -> dict:
    """Run materialization for all tenants in a workspace.

    Writes progress to ``MaterializationRun.progress`` after each page so
    the MCP polling loop can surface real-time status to the user. The
    ``progress_updater`` closure also acts as the cancellation checkpoint:
    it re-reads ``MaterializationRun.state`` and raises
    ``MaterializationCancelled`` when the run has been marked CANCELLED
    by the cancel endpoint, triggering a transaction rollback.

    Returns a per-tenant summary so the polling loop can build a final
    aggregated result for the agent.
    """
    job_id = context.job.id

    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.exception("materialize_workspace: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}

    qs = TenantMembership.objects.select_related("user", "tenant").filter(
        tenant_id__in=[
            wt.tenant_id
            async for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related(
                "tenant"
            )
        ]
    )
    if user_id:
        qs = qs.filter(user_id=user_id)

    memberships = [tm async for tm in qs]
    if not memberships:
        logger.warning("materialize_workspace: no memberships for workspace %s", workspace_id)
        return {"error": "No tenant memberships found", "tenants": []}

    registry = get_registry()
    provider_pipeline_map = {p.provider: p.name for p in registry.list()}
    tenant_results: list[dict] = []

    for tm in memberships:
        tenant_id = tm.tenant.external_id
        pipeline_name = provider_pipeline_map.get(tm.tenant.provider)
        if pipeline_name is None:
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "success": False,
                    "error": f"No pipeline for provider '{tm.tenant.provider}'",
                }
            )
            continue

        credential = await aresolve_credential(tm)
        if credential is None:
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "success": False,
                    "error": "No credential configured",
                }
            )
            continue

        pipeline_config = registry.get(pipeline_name)
        try:
            result = await asyncio.to_thread(
                _run_pipeline_with_progress,
                tm,
                credential,
                pipeline_config,
                job_id,
            )
            tenant_results.append({"tenant": tenant_id, "success": True, "result": result})
        except MaterializationCancelled:
            tenant_results.append({"tenant": tenant_id, "success": False, "cancelled": True})
            # Stop processing remaining tenants — the user has cancelled.
            break
        except Exception as e:
            logger.exception("Materialization failed for tenant %s", tenant_id)
            tenant_results.append({"tenant": tenant_id, "success": False, "error": str(e)})

    # Chain the resume task so the agent picks up where it left off.
    try:
        tj = await ThreadJob.objects.filter(procrastinate_job_id=job_id).afirst()
        if tj is None:
            # MCP may still be committing the ThreadJob row (it dispatches
            # the procrastinate job before creating the tracking row).
            # Retry once after a short delay to close the race.
            await asyncio.sleep(0.5)
            tj = await ThreadJob.objects.filter(procrastinate_job_id=job_id).afirst()
        if tj is not None:
            await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
    except Exception:
        logger.exception("Failed to defer resume task for job %s", job_id)

    return {
        "tenants": tenant_results,
        "all_succeeded": all(r.get("success") for r in tenant_results),
    }


def _run_pipeline_with_progress(
    tenant_membership,
    credential: dict,
    pipeline_config,
    job_id: int,
) -> dict:
    """Synchronous entry point invoked under ``asyncio.to_thread``.

    Builds the ``progress_updater`` closure that mirrors progress to the DB
    and surfaces cancellation, then runs the pipeline. Exceptions propagate
    to the caller.
    """

    def updater(progress: dict) -> None:
        run_id = progress.get("run_id")
        if run_id is None:
            return
        MaterializationRun.objects.filter(id=run_id).update(progress=progress)
        current_state = (
            MaterializationRun.objects.filter(id=run_id).values_list("state", flat=True).first()
        )
        if current_state == MaterializationRun.RunState.CANCELLED:
            raise MaterializationCancelled()

    return run_pipeline(
        tenant_membership,
        credential,
        pipeline_config,
        progress_updater=updater,
        procrastinate_job_id=job_id,
    )


async def _drop_schema_and_fail(schema) -> None:
    """Drop the physical schema and mark the record as FAILED."""
    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown, schema)
    except Exception:
        logger.exception("Failed to drop schema '%s' during cleanup", schema.schema_name)
    schema.state = SchemaState.FAILED
    await schema.asave(update_fields=["state"])


@app.periodic(cron="*/30 * * * *")
@app.task
async def expire_inactive_schemas(timestamp: int = 0) -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Handles both TenantSchema and WorkspaceViewSchema records.
    Schemas with null last_accessed_at are never auto-expired.

    `timestamp` is supplied by the procrastinate periodic deferrer; the default
    lets tests invoke this task directly.
    """
    cutoff = timezone.now() - timedelta(hours=settings.SCHEMA_TTL_HOURS)

    # Expire stale tenant schemas
    async for schema in TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        schema.state = SchemaState.TEARDOWN
        await schema.asave(update_fields=["state"])
        await teardown_schema.defer_async(schema_id=str(schema.id))

    # Expire stale view schemas
    async for vs in WorkspaceViewSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        vs.state = SchemaState.TEARDOWN
        await vs.asave(update_fields=["state"])
        await teardown_view_schema_task.defer_async(view_schema_id=str(vs.id))


@app.task
async def rebuild_workspace_view_schema(workspace_id: str) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace.

    On success: marks WorkspaceViewSchema.state = ACTIVE.
    On failure: marks state = FAILED and returns an error dict.
    """
    try:
        workspace = await Workspace.objects.prefetch_related("tenants").aget(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.exception("rebuild_workspace_view_schema: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}

    manager = SchemaManager()
    try:
        vs = await asyncio.to_thread(manager.build_view_schema, workspace)
    except Exception:
        # build_view_schema already saves state=FAILED before re-raising;
        # no need to write it again here (doing so risks overwriting a
        # concurrent state transition, e.g. TEARDOWN set by expire_inactive_schemas).
        logger.exception("Failed to build view schema for workspace %s", workspace_id)
        return {"error": "Failed to build view schema"}

    logger.info(
        "View schema '%s' is now active for workspace %s",
        vs.schema_name,
        workspace_id,
    )
    return {"status": "active", "schema_name": vs.schema_name}


@app.task
async def teardown_view_schema_task(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    try:
        vs = await WorkspaceViewSchema.objects.aget(id=view_schema_id)
    except WorkspaceViewSchema.DoesNotExist:
        logger.exception("teardown_view_schema_task: view schema %s not found", view_schema_id)
        return

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown_view_schema, vs)
    except Exception:
        logger.exception("Failed to drop view schema '%s'", vs.schema_name)
        vs.state = SchemaState.ACTIVE
        await vs.asave(update_fields=["state"])
        raise

    vs.state = SchemaState.EXPIRED
    await vs.asave(update_fields=["state"])


@app.task
async def teardown_schema(schema_id: str) -> None:
    """Drop a tenant schema in the managed database and mark it EXPIRED."""
    try:
        schema = await TenantSchema.objects.aget(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.exception("teardown_schema: schema %s not found", schema_id)
        return

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown, schema)
    except Exception:
        # teardown() only raises when DROP SCHEMA itself fails — role-cleanup
        # failures are logged and swallowed there — so reaching this branch
        # means the physical schema is still present and the record should go
        # back to ACTIVE rather than being stranded in TEARDOWN.
        schema.state = SchemaState.ACTIVE
        await schema.asave(update_fields=["state"])
        raise

    try:
        schema.state = SchemaState.EXPIRED
        await schema.asave(update_fields=["state"])
    except Exception:
        # Physical schema is already dropped; don't pretend it's ACTIVE.
        logger.exception(
            "teardown_schema: failed to mark schema %s EXPIRED after teardown", schema.id
        )
        raise


STALE_JOB_THRESHOLD = timedelta(hours=1)


async def _procrastinate_job_active(job_id: int) -> bool:
    """Return True if the given procrastinate job is still in 'todo' or 'doing'."""
    try:
        status = await current_app.job_manager.get_job_status_async(job_id)
    except Exception:
        return False
    return status.value in {"todo", "doing"}


@app.periodic(cron="*/15 * * * *")
@app.task
async def expire_stale_thread_jobs(timestamp: int = 0) -> dict:
    """Flip ThreadJobs that have been active too long and whose procrastinate
    job is no longer running. Fires the resume task so the user is not stuck
    with a phantom spinner.
    """
    cutoff = timezone.now() - STALE_JOB_THRESHOLD
    flipped = 0
    async for tj in ThreadJob.objects.filter(
        state__in=list(ThreadJob.ACTIVE_STATES),
        created_at__lt=cutoff,
    ):
        if await _procrastinate_job_active(tj.procrastinate_job_id):
            continue
        if tj.state == ThreadJob.State.RUNNING:
            # A worker started a resume and presumably crashed mid-ainvoke.
            # Marking FAILED directly avoids deferring a duplicate resume that
            # could race with a still-running first invocation.
            updated = await ThreadJob.objects.filter(
                id=tj.id, state=ThreadJob.State.RUNNING,
            ).aupdate(state=ThreadJob.State.FAILED, completed_at=timezone.now())
            if updated:
                flipped += 1
                logger.warning(
                    "Janitor: ThreadJob %s stuck in RUNNING (worker crash?); marked FAILED",
                    tj.id,
                )
            continue
        # PENDING stuck job — never claimed by any worker. Safe to defer a fresh
        # resume so the user gets an agent follow-up.
        try:
            await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
            flipped += 1
        except Exception:
            logger.exception("Janitor: failed to defer resume for %s", tj.id)
            await ThreadJob.objects.filter(id=tj.id).aupdate(
                state=ThreadJob.State.FAILED, completed_at=timezone.now(),
            )
    return {"flipped": flipped}


async def _build_agent_for_resume(workspace, user):
    """Build the LangGraph agent + load oauth_tokens for runtime config.

    Returns (agent, oauth_tokens).
    """
    mcp_tools = await get_mcp_tools()
    oauth_tokens = await get_user_oauth_tokens(user)
    checkpointer = await ensure_checkpointer()
    agent = await build_agent_graph(
        workspace=workspace,
        user=user,
        checkpointer=checkpointer,
        mcp_tools=mcp_tools,
        oauth_tokens=oauth_tokens,
    )
    return agent, oauth_tokens


async def _aggregate_materialization_state(procrastinate_job_id: int) -> tuple[str, list[dict]]:
    """Inspect MaterializationRun rows for this job, return (status, per-tenant summary)."""
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=procrastinate_job_id,
        ).select_related("tenant_schema__tenant")
    ]
    if not runs:
        return "no_runs", []
    summary: list[dict] = []
    any_cancelled = False
    any_failed = False
    all_completed = True
    for r in runs:
        tenant_id = r.tenant_schema.tenant.external_id
        row_counts: dict = {}
        if isinstance(r.result, dict):
            for source, info in (r.result.get("sources") or {}).items():
                if isinstance(info, dict) and "rows" in info:
                    row_counts[source] = info["rows"]
        summary.append({
            "tenant": tenant_id,
            "state": r.state,
            "row_counts": row_counts,
        })
        if r.state == MaterializationRun.RunState.CANCELLED:
            any_cancelled = True
            all_completed = False
        elif r.state == MaterializationRun.RunState.FAILED:
            any_failed = True
            all_completed = False
        elif r.state != MaterializationRun.RunState.COMPLETED:
            all_completed = False
    status = (
        "cancelled" if any_cancelled
        else ("failed" if any_failed else ("completed" if all_completed else "partial"))
    )
    return status, summary


@app.task(pass_context=True)
async def resume_thread_after_materialization(context, thread_job_id: str) -> dict:
    """Inject a system-framed message into the LangGraph conversation and
    re-invoke the agent so it can respond to the original request with the
    now-loaded data.
    """
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

    # CLAIMABLE_STATES excludes RUNNING because aupdate() returns the count of
    # rows MATCHED (not changed). Including RUNNING would let a second
    # concurrent invocation re-claim an already-running ThreadJob and produce
    # a duplicate agent.ainvoke() against the same LangGraph thread.
    # CANCELLED is intentionally included so the agent can compose a follow-up
    # message even for cancelled materializations.
    CLAIMABLE_STATES = [ThreadJob.State.PENDING, ThreadJob.State.CANCELLED]
    claimed = await ThreadJob.objects.filter(
        id=tj.id, state__in=CLAIMABLE_STATES,
    ).aupdate(state=ThreadJob.State.RUNNING)
    if not claimed:
        logger.info("resume: ThreadJob %s already claimed; no-op", thread_job_id)
        return {"status": "already_claimed"}

    status, summary = await _aggregate_materialization_state(tj.procrastinate_job_id)
    # If the job was loaded as CANCELLED (user clicked Stop before we ran),
    # force status to "cancelled" regardless of what _aggregate_materialization_state
    # found — the user's intent wins.
    if tj.state == ThreadJob.State.CANCELLED:
        status = "cancelled"

    if status == "no_runs":
        logger.warning(
            "resume: no MaterializationRun rows for ThreadJob %s job_id=%s; "
            "invoking agent with explanation so the user is not left with a spinner",
            thread_job_id, tj.procrastinate_job_id,
        )
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization finished without running any "
            f"pipelines. This typically means the workspace's tenants have no "
            f"pipeline configured or no credentials set up. Please tell the user "
            f"what happened and suggest checking the workspace's connection."
        )
    else:
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization just completed "
            f"(status={status}). Please continue with the user's original request "
            f"using the now-loaded data. Per-tenant: {summary}"
        )

    workspace = tj.thread.workspace
    user = tj.thread.user

    try:
        agent, oauth_tokens = await _build_agent_for_resume(workspace, user)
        input_state = {
            "messages": [HumanMessage(content=body)],
            "workspace_id": str(workspace.id),
            "user_id": str(user.id),
            "user_role": "analyst",
            "thread_id": str(tj.thread.id),
        }
        config = {
            "configurable": {"thread_id": str(tj.thread.id)},
            "recursion_limit": 50,
            "oauth_tokens": oauth_tokens,
        }
        await agent.ainvoke(input_state, config)
    except Exception:
        logger.exception(
            "resume: agent build or invoke failed for thread_job %s", thread_job_id
        )
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
        )
        return {"status": "agent_failed"}

    # Bump Thread.updated_at so the sidebar's "newer than last_viewed" check
    # fires the green-dot indicator after a successful background resume.
    # Isolated try/except: a DB failure here must not contaminate the
    # success path (the agent message was already persisted via ainvoke).
    try:
        await Thread.objects.filter(id=tj.thread_id).aupdate(updated_at=timezone.now())
    except Exception:
        logger.warning(
            "resume: Thread.updated_at bump failed for thread %s; "
            "green-dot indicator may not fire",
            tj.thread_id, exc_info=True,
        )

    terminal = (
        ThreadJob.State.CANCELLED if status == "cancelled"
        else (
            ThreadJob.State.FAILED if status in ("failed", "partial", "no_runs")
            else ThreadJob.State.COMPLETED
        )
    )
    await ThreadJob.objects.filter(id=tj.id).aupdate(
        state=terminal, completed_at=timezone.now(),
    )
    return {"status": "resumed", "terminal_state": terminal}
