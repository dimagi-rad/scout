"""Background tasks for schema lifecycle management."""

import asyncio
import contextlib
import logging
import time
from datetime import timedelta

import sentry_sdk
from django.conf import settings
from django.db import close_old_connections
from django.db.models import Count, OuterRef, Q, Subquery
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.agents.graph.base import build_agent_graph
from apps.agents.mcp_client import get_mcp_tools, get_user_oauth_tokens
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.chat.models import Thread, ThreadJob
from apps.users.models import TenantMembership
from apps.users.services.credential_resolver import aresolve_credential
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager
from config.procrastinate import app, task
from mcp_server.loaders.connect_base import ConnectExportError
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import (
    MaterializationCancelled,
    run_pipeline,
)

try:
    from langfuse import Langfuse
except ImportError:  # langfuse is an optional observability dependency
    Langfuse = None

# User-facing failure copy. The frontend renders these straight from the
# checkpointer (apps/chat/thread_views.py:_load_thread_messages → AIMessage).
RESUME_TIMEOUT_MESSAGE = (
    "The agent took too long to respond after materialization completed. "
    "Please re-ask your question."
)
RESUME_EXCEPTION_MESSAGE = "Sorry, something went wrong while preparing your answer. Please retry."
RESUME_STUCK_RUNNING_MESSAGE = (
    "Your materialization completed but the follow-up response was interrupted "
    "(likely a server restart). Please re-ask your question."
)
MATERIALIZATION_FAILED_MESSAGE = (
    "Data loading failed before it could complete. Please retry your request."
)

logger = logging.getLogger(__name__)


def _compose_failure_summary(runs: list[MaterializationRun]) -> str:
    """Compose a human-readable failure summary from MaterializationRun results.

    Used to populate ``ThreadJob.error_summary`` so the frontend can render an
    inline failure card after the spinner clears. Reads the per-source state
    map in ``run.result["sources"]`` (post-#198 shape) and produces a short
    string that names what failed and what (if anything) loaded.

    Returns "" when there are no runs or the result map is empty — callers
    should fall back to a generic message in that case.
    """
    if not runs:
        return ""

    failed_sources: list[tuple[str, str]] = []
    completed_sources: list[tuple[str, int]] = []
    skipped_sources: list[str] = []
    cancelled_sources: list[str] = []

    for run in runs:
        result = run.result if isinstance(run.result, dict) else None
        if not result:
            continue
        for name, info in (result.get("sources") or {}).items():
            if not isinstance(info, dict):
                continue
            state = info.get("state")
            if state == "failed":
                failed_sources.append((name, str(info.get("error") or "unknown error")))
            elif state == "completed":
                completed_sources.append((name, int(info.get("rows") or 0)))
            elif state == "skipped":
                skipped_sources.append(name)
            elif state == "cancelled":
                cancelled_sources.append(name)

    parts: list[str] = []
    if failed_sources:
        first = failed_sources[0]
        if len(failed_sources) == 1:
            parts.append(f"{first[0]} failed: {first[1]}")
        else:
            others = ", ".join(n for n, _ in failed_sources[1:])
            parts.append(f"{first[0]} failed ({first[1]}); also failed: {others}")
    if completed_sources:
        total_rows = sum(rows for _, rows in completed_sources)
        names = ", ".join(n for n, _ in completed_sources)
        parts.append(f"{names} ({total_rows:,} rows) loaded successfully")
    if skipped_sources:
        parts.append(f"remaining sources skipped: {', '.join(skipped_sources)}")
    if cancelled_sources:
        parts.append(f"cancelled: {', '.join(cancelled_sources)}")

    if not parts:
        # No per-source detail (e.g. failure before any source ran). Surface
        # the run state so the message is non-empty.
        states = sorted({r.state for r in runs})
        return f"Materialization {'/'.join(states)}."
    return ". ".join(parts) + "."


@task
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
        membership = await TenantMembership.objects.select_related(
            "tenant", "user", "connection"
        ).aget(id=membership_id)
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

    # Step 2: Resolve credential and run materialization pipeline. This task
    # runs as an async job, so it must use the async resolver — the sync one
    # issues ORM queries that raise SynchronousOnlyOperation here.
    credential = await aresolve_credential(membership)
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
        # Load into the new "_r" schema this task created — NOT the tenant's
        # base schema. Without target_schema, run_pipeline re-resolves the base
        # (old active) schema via provision() and the data lands there; we then
        # activate this empty new schema and tear down the data-bearing old one.
        await asyncio.to_thread(
            run_pipeline, membership, credential, pipeline_config, target_schema=new_schema
        )
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        await _drop_schema_and_fail(new_schema)
        return {"error": "Materialization failed"}

    # Step 3: Mark new schema as active. Set last_accessed_at to now so the
    # freshly materialized schema starts with a clean inactivity TTL —
    # otherwise expire_inactive_schemas could drop it before it is ever used.
    new_schema.state = SchemaState.ACTIVE
    new_schema.last_accessed_at = timezone.now()
    await new_schema.asave(update_fields=["state", "last_accessed_at"])

    # Step 3b: The tenant data schema is SHARED across workspaces, and this refresh
    # swapped in a NEW physical schema. Every multi-tenant WorkspaceViewSchema that
    # includes this tenant still points its namespaced views at the OLD schema
    # (about to be torn down), so defer a rebuild for each so their views are
    # recreated against the new ACTIVE schema — mirroring the sibling rebuild
    # materialize_workspace performs (PR #230). Without this the views keep serving
    # stale data until teardown drops the old schema, after which they'd be left
    # empty (and falsely marked FAILED by teardown_schema).
    await _rebuild_dependent_view_schemas([new_schema.tenant_id])

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


@task(pass_context=True)
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
    tenant_results: list[dict] = []

    try:
        try:
            workspace = await Workspace.objects.aget(id=workspace_id)
        except Workspace.DoesNotExist:
            logger.exception("materialize_workspace: workspace %s not found", workspace_id)
            return {"error": "Workspace not found"}

        qs = TenantMembership.objects.select_related("user", "tenant", "connection").filter(
            archived_at__isnull=True,
            tenant_id__in=[
                wt.tenant_id
                async for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related(
                    "tenant"
                )
            ],
        )
        if user_id:
            qs = qs.filter(user_id=user_id)

        memberships = [tm async for tm in qs]
        if not memberships:
            logger.warning("materialize_workspace: no memberships for workspace %s", workspace_id)
            return {"error": "No tenant memberships found", "tenants": []}

        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}

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
            except ConnectExportError as e:
                # Upstream Connect failure after retry exhaustion. Capture
                # the response's sentry-trace header so support can correlate
                # with Connect's Sentry in a single hop. sentry_sdk.set_tag
                # is a no-op when the SDK was never initialised (no DSN).
                logger.exception(
                    "Materialization failed for tenant %s on pipeline %s: "
                    "connect status=%s after %d attempts (last_id=%s, sentry-trace=%s)",
                    tenant_id,
                    pipeline_name,
                    e.status,
                    e.attempts,
                    e.last_id,
                    e.sentry_trace,
                )
                sentry_sdk.set_tag("connect.upstream_sentry_trace", e.sentry_trace or "")
                sentry_sdk.set_tag("connect.pipeline", pipeline_name or "")
                tenant_results.append({"tenant": tenant_id, "success": False, "error": str(e)})
            except Exception as e:
                logger.exception("Materialization failed for tenant %s", tenant_id)
                tenant_results.append({"tenant": tenant_id, "success": False, "error": str(e)})

        all_succeeded = all(r.get("success") for r in tenant_results)

        # Multi-tenant workspaces query through a WorkspaceViewSchema that
        # UNION ALLs the per-tenant tables. build_view_schema requires every
        # tenant to have an ACTIVE TenantSchema, so we can only attempt this
        # after the per-tenant loop completes successfully. This must run
        # *before* the resume task fires (deferred in the finally block) so
        # the agent's first list_tables call after materialization returns
        # the namespaced view instead of "No active view schema for workspace".
        view_schema_outcome: dict | None = None
        workspace_tenant_count = await workspace.workspace_tenants.acount()
        if workspace_tenant_count > 1 and all_succeeded:
            try:
                await asyncio.to_thread(SchemaManager().build_view_schema, workspace)
                view_schema_outcome = {"ok": True, "error": None}
            except Exception as exc:
                # Don't re-raise — the resume task should still fire so the
                # user gets *some* agent response. The failure is recorded on
                # the WorkspaceViewSchema row (state=FAILED, last_error) and is
                # surfaced to the agent by the resume task, which inspects the
                # row directly rather than relying on this return value.
                logger.exception(
                    "Post-materialization view schema rebuild failed for workspace %s",
                    workspace_id,
                )
                view_schema_outcome = {"ok": False, "error": str(exc)[:500]}

        # Tenant data schemas (t_<id>) are SHARED across workspaces. Re-materializing
        # a tenant from THIS workspace drops & recreates its raw_* tables, which
        # cascade-drops the namespaced views inside every OTHER workspace's view
        # schema — leaving those WorkspaceViewSchema rows ACTIVE but empty. Defer a
        # rebuild for each sibling multi-tenant workspace that shares any tenant we
        # just (re)materialized so their views are recreated against the new tables.
        # Failures of individual rebuilds are handled inside rebuild_workspace_view_schema;
        # we never block the resume on them.
        await _rebuild_dependent_view_schemas(
            [tm.tenant_id for tm in memberships],
            exclude_workspace_id=str(workspace.id),
        )

        return {
            "tenants": tenant_results,
            "all_succeeded": all_succeeded,
            "view_schema": view_schema_outcome,
        }
    finally:
        # ALWAYS defer the resume task so the user is not left with a phantom
        # spinner — even on early-return paths (workspace missing, no
        # memberships) where the loop above never executed.
        await _defer_resume_for_job(job_id)


async def _defer_resume_for_job(job_id: int) -> None:
    """Find the ThreadJob bound to ``job_id`` and defer the resume task.

    MCP commits the ThreadJob row *after* defer_async returns the procrastinate
    job id (see mcp_server.server.run_materialization), so under load the
    worker may finish before the row is visible. Hedge with a bounded backoff:
    total budget ~3.75s, which is acceptable because procrastinate workers
    handle one task at a time per slot. If the row still is not visible after
    retries, the janitor (expire_stale_thread_jobs) catches up eventually.

    TODO: a cleaner fix is to let MCP write a placeholder ThreadJob *before*
    defer_async, then patch in the procrastinate_job_id after dispatch. That
    requires making procrastinate_job_id nullable (a migration we are
    skipping for this PR).
    """
    try:
        tj = None
        for delay in (0, 0.25, 0.5, 1.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            tj = await ThreadJob.objects.filter(procrastinate_job_id=job_id).afirst()
            if tj is not None:
                break
        if tj is None:
            logger.warning(
                "materialize_workspace: no ThreadJob found for job_id %s after retries; "
                "janitor will catch up if MCP eventually commits one",
                job_id,
            )
            return
        await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
    except Exception:
        logger.exception("Failed to defer resume task for job %s", job_id)


def _multi_tenant_count_subquery():
    """Correlated subquery yielding a workspace's total tenant count.

    A plain ``annotate(Count("workspace_tenants"))`` shares the same join as a
    ``filter(workspace_tenants__tenant_id__in=...)`` predicate, so the count
    collapses to only the *filtered* tenants (always 1 here) — the classic
    Django filter+aggregate-on-the-same-multivalued-relation trap. Counting via
    an independent subquery over the junction sidesteps that and stays a single
    SQL round-trip (no per-tenant N+1).
    """
    return Subquery(
        WorkspaceTenant.objects.filter(workspace=OuterRef("pk"))
        .order_by()
        .values("workspace")
        .annotate(n=Count("id"))
        .values("n")
    )


def _dependent_view_schema_workspaces(tenant_ids, exclude_workspace_id=None):
    """Queryset of multi-tenant workspaces with a WorkspaceViewSchema row that
    share any of ``tenant_ids``.

    A workspace qualifies when it (i) contains at least one of the given tenants,
    (ii) is multi-tenant (>= 2 tenants), and (iii) has a WorkspaceViewSchema row
    in any state. When ``exclude_workspace_id`` is given, that workspace is left
    out — used by the materialize path, which rebuilds its own view schema inline
    and only needs to fan out to the *siblings*. The refresh/teardown paths pass
    no exclusion because they are not scoped to a workspace.

    Uses a single annotated query (a subquery tenant count) rather than walking
    each tenant's workspaces, so cost is independent of the number of tenants
    materialized (no N+1).
    """
    qs = Workspace.objects.filter(
        workspace_tenants__tenant_id__in=tenant_ids,
        view_schema__isnull=False,
    )
    if exclude_workspace_id is not None:
        qs = qs.exclude(id=exclude_workspace_id)
    return (
        qs.annotate(num_tenants=_multi_tenant_count_subquery())
        .filter(num_tenants__gte=2)
        .distinct()
    )


async def _rebuild_dependent_view_schemas(tenant_ids, *, exclude_workspace_id=None) -> None:
    """Defer a view-schema rebuild for every multi-tenant workspace whose
    namespaced views were (or will be) cascade-dropped by a (re-)materialization
    or refresh of one of ``tenant_ids``.

    Shared by materialize_workspace (which excludes the current workspace it
    already rebuilt inline) and the refresh/teardown paths (which exclude
    nothing). The query already yields distinct workspace ids, so no extra dedupe
    is needed. Best-effort: a failure to defer one rebuild must not block the
    caller, so each defer is individually guarded and the dispatched task owns its
    own failure handling.
    """
    async for ws_id in (
        _dependent_view_schema_workspaces(tenant_ids, exclude_workspace_id)
        .values_list("id", flat=True)
        .aiterator()
    ):
        try:
            await rebuild_workspace_view_schema.defer_async(workspace_id=str(ws_id))
        except Exception:
            logger.exception(
                "Failed to defer dependent view-schema rebuild for workspace %s", ws_id
            )


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
    # This runs on an asyncio.to_thread executor thread, which has its own
    # thread-local Django connection that the task decorator's close_old_connections (which
    # cleans the async-ORM thread) cannot reach. Pool threads are reused
    # across jobs, so a connection that died since the last pipeline run here
    # would otherwise poison every progress update.
    close_old_connections()

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
@task
async def expire_inactive_schemas(timestamp: int = 0) -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Handles both TenantSchema and WorkspaceViewSchema records.
    Schemas with null last_accessed_at are never auto-expired.

    The data-bearing MaterializationRun rows are NOT touched here. A schema in
    TEARDOWN is already unreachable via the catalog (load_tenant_context only
    resolves ACTIVE/MATERIALIZING schemas), so flipping runs to STALE before the
    physical DROP succeeds buys nothing — and if teardown_schema later fails the
    DROP and reverts the schema to ACTIVE, prematurely-staled runs would strand
    the (still-present) data as invisible. teardown_schema marks the runs STALE
    only after manager.teardown actually drops the schema.

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


@task
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


@task
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


@task
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
        # back to ACTIVE rather than being stranded in TEARDOWN. The
        # data-bearing runs are deliberately left in their terminal
        # COMPLETED/PARTIAL state: the physical tables still exist, so the
        # catalog must keep surfacing them once the schema is ACTIVE again.
        schema.state = SchemaState.ACTIVE
        await schema.asave(update_fields=["state"])
        raise

    # The physical schema (and its tables) is now dropped. Flip the data-bearing
    # runs to STALE so pipeline_list_tables stops returning ghost entries for
    # tables that no longer exist. This is done here — after the DROP succeeds —
    # rather than at TEARDOWN-flip time, so a failed DROP never strands intact
    # data as invisible. CANCELLED/FAILED runs are already terminal and excluded
    # from the catalog query, so they're left alone.
    await MaterializationRun.objects.filter(
        tenant_schema=schema,
        state__in=[
            MaterializationRun.RunState.COMPLETED,
            MaterializationRun.RunState.PARTIAL,
        ],
    ).aupdate(state=MaterializationRun.RunState.STALE)

    # The tenant schema (t_<id>) is SHARED across workspaces. DROP SCHEMA ... CASCADE
    # just cascade-dropped the namespaced views inside every dependent multi-tenant
    # workspace's view schema (ws_<hash>). What to do next depends on whether the
    # tenant still has data: a refresh swaps in a NEW ACTIVE schema before tearing
    # down the old one (data intact → rebuild), whereas pure TTL expiry leaves the
    # tenant with nothing (data gone → fail). _reconcile handles both.
    await _reconcile_dependent_view_schemas_after_teardown(schema)

    try:
        schema.state = SchemaState.EXPIRED
        await schema.asave(update_fields=["state"])
    except Exception:
        # Physical schema is already dropped; don't pretend it's ACTIVE.
        logger.exception(
            "teardown_schema: failed to mark schema %s EXPIRED after teardown", schema.id
        )
        raise


async def _reconcile_dependent_view_schemas_after_teardown(schema) -> None:
    """Reconcile dependent multi-tenant view schemas after ``schema`` is dropped.

    DROP SCHEMA ... CASCADE just cascade-dropped the namespaced views inside every
    dependent multi-tenant workspace's view schema. The correct follow-up depends
    on whether the tenant still has data:

    - If the tenant has ANOTHER ACTIVE schema (the refresh path: a fresh schema was
      swapped in before this old one was torn down), the data is NOT gone and the
      views are rebuildable — defer a rebuild so they point at the new schema.
    - If the tenant has NO surviving ACTIVE schema (pure TTL expiry), the data is
      gone; flip the dependent ACTIVE view schemas to FAILED so the catalog reports
      the truth rather than serving an empty view.

    The ``exclude(id=schema.id)`` matters: production callers flip ``schema`` to
    TEARDOWN before dispatching teardown, but a direct caller may pass an ACTIVE
    row — excluding it makes "another ACTIVE schema?" correct either way.
    """
    tenant_has_surviving_active_schema = (
        await TenantSchema.objects.filter(
            tenant_id=schema.tenant_id,
            state=SchemaState.ACTIVE,
        )
        .exclude(id=schema.id)
        .aexists()
    )
    if tenant_has_surviving_active_schema:
        await _rebuild_dependent_view_schemas([schema.tenant_id])
    else:
        await _fail_dependent_view_schemas(schema.tenant_id)


async def _fail_dependent_view_schemas(tenant_id) -> int:
    """Flip every ACTIVE WorkspaceViewSchema that depends on ``tenant_id`` to FAILED.

    A view schema depends on a tenant when its workspace is multi-tenant (>= 2
    tenants) and contains that tenant: its namespaced views were just
    cascade-dropped by the tenant-schema DROP. We only touch ACTIVE rows — rows
    already in TEARDOWN/FAILED/EXPIRED must not be clobbered out of their
    lifecycle state. A single annotated subquery scopes the update to the right
    workspaces, so cost is independent of how many workspaces share the tenant.

    Returns the number of rows flipped.
    """
    dependent_workspace_ids = (
        Workspace.objects.filter(workspace_tenants__tenant_id=tenant_id)
        .annotate(num_tenants=_multi_tenant_count_subquery())
        .filter(num_tenants__gte=2)
        .values("id")
    )
    return await WorkspaceViewSchema.objects.filter(
        workspace_id__in=dependent_workspace_ids,
        state=SchemaState.ACTIVE,
    ).aupdate(state=SchemaState.FAILED)


STALE_JOB_THRESHOLD = timedelta(minutes=10)


def _staleness_anchor(tj: ThreadJob):
    """Timestamp from which a ThreadJob's staleness is measured.

    For a RUNNING job (a resume task has claimed it) we measure from the RESUME
    phase: ``started_at``. created_at includes the full materialization + queue
    time, so a healthy long materialization (>10 min) followed by a fresh resume
    would otherwise look stale the instant the resume began — the false-positive
    the reconciler used to hit (finding 02#9).

    For PENDING/other states (or a legacy RUNNING row predating ``started_at``)
    we fall back to ``created_at`` so a job that was never claimed still ages
    out and a never-recorded resume is not stranded forever.
    """
    if tj.state == ThreadJob.State.RUNNING and tj.started_at is not None:
        return tj.started_at
    return tj.created_at


def _stale_active_jobs_q(cutoff) -> Q:
    """Predicate matching active ThreadJobs whose effective staleness anchor is
    older than ``cutoff`` (see :func:`_staleness_anchor`).

    A RUNNING job with a recorded ``started_at`` is stale only when that resume
    phase is older than the cutoff; everything else (PENDING, or a legacy
    RUNNING row with no ``started_at``) is measured from ``created_at``. Keeping
    this as a single ORM-side predicate means the janitor never even SELECTs a
    healthy in-flight resume, avoiding the 02#9 false-positive at the source.
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

    Reads the status directly from the ``procrastinate_jobs`` table via the
    Django contrib ORM model rather than ``current_app.job_manager``. The
    module-level ``current_app`` reference resolves to procrastinate's
    ``FutureApp`` blueprint proxy at import time; ``AppConfig.ready()`` rebinds
    the name in procrastinate's own module to the real ``App``, but our
    already-imported reference keeps pointing at the unresolved ``FutureApp``,
    which has no ``job_manager`` (it is a ``Blueprint``). In the worker that
    raised ``AttributeError`` on every call, so the janitor treated every
    lookup as "couldn't tell" and never reconciled. The ORM model sidesteps the
    app lifecycle entirely and is async-native.

    Returning a sentinel on exception would conflate "not active" with
    "couldn't tell" — the janitor would then misclassify actively-running jobs
    as candidates for cleanup during a transient DB blip. Callers must treat
    ``None`` as "don't touch this row this tick" (also returned for an unknown
    job id, where there is nothing to reconcile against).
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
        # Status unknown (probably a transient DB error). Don't touch the
        # row this tick — the next invocation will retry. This prevents
        # incorrectly cleaning up jobs that may still be running.
        return None
    if status in {"todo", "doing"}:
        return None
    if tj.state == ThreadJob.State.RUNNING:
        # A RUNNING ThreadJob means a resume task claimed it. The materialize
        # job's status is irrelevant here (it has long since succeeded for any
        # >10 min materialization). Measure staleness from the RESUME phase
        # (started_at) so we only flip a resume that has genuinely been stuck
        # past the threshold — NOT a healthy resume that just started after a
        # long materialization (finding 02#9). A fresh resume is left alone.
        anchor = _staleness_anchor(tj)
        if anchor is not None and timezone.now() - anchor < STALE_JOB_THRESHOLD:
            return None
        # A worker started a resume and presumably crashed mid-ainvoke.
        # Marking FAILED directly avoids deferring a duplicate resume that
        # could race with a still-running first invocation.
        updated = await ThreadJob.objects.filter(
            id=tj.id,
            state=ThreadJob.State.RUNNING,
        ).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
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
        # Surface the crash to the user via a synthetic AIMessage; the
        # checkpointer write tolerates failure so a sick worker
        # doesn't block the FAILED transition.
        await _persist_synthetic_failure_message(tj, RESUME_STUCK_RUNNING_MESSAGE)
        return "failed"
    if status in {"failed", "aborted"}:
        # The task itself raised (e.g. the worker's DB connection died before
        # any pipeline ran) — there is no result for an agent resume to
        # narrate. Flip straight to FAILED so the failure reaches the user
        # instead of waiting on a resume that has nothing to say.
        summary = await _build_failure_summary_for_job(tj.procrastinate_job_id)
        updated = await ThreadJob.objects.filter(
            id=tj.id,
            state=ThreadJob.State.PENDING,
        ).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            error_summary=summary or MATERIALIZATION_FAILED_MESSAGE,
        )
        if not updated:
            return None
        logger.warning(
            "Reconcile: ThreadJob %s pending but procrastinate job %s is %s; marked FAILED",
            tj.id,
            tj.procrastinate_job_id,
            status,
        )
        await _persist_synthetic_failure_message(tj, MATERIALIZATION_FAILED_MESSAGE)
        return "failed"
    # PENDING stuck job whose materialization finished (succeeded/cancelled)
    # — never claimed by any worker. Safe to defer a fresh resume so the user
    # gets an agent follow-up; the resume task flips the state.
    try:
        await resume_thread_after_materialization.defer_async(thread_job_id=str(tj.id))
    except Exception:
        logger.exception("Reconcile: failed to defer resume for %s", tj.id)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
            error_summary=(
                "Background queue unavailable; the materialization could "
                "not be resumed. Please retry."
            ),
        )
        return "fallback_failed"
    return "resumed"


@app.periodic(cron="*/15 * * * *")
@task
async def expire_stale_thread_jobs(timestamp: int = 0) -> dict:
    """Flip ThreadJobs that have been active too long and whose procrastinate
    job is no longer running. Fires the resume task so the user is not stuck
    with a phantom spinner.
    """
    cutoff = timezone.now() - STALE_JOB_THRESHOLD
    flipped = 0
    async for tj in ThreadJob.objects.select_related(
        "thread__workspace",
        "thread__user",
    ).filter(_stale_active_jobs_q(cutoff)):
        action = await reconcile_stale_thread_job(tj)
        if action in {"failed", "resumed"}:
            flipped += 1
    return {"flipped": flipped}


async def _build_failure_summary_for_job(procrastinate_job_id: int) -> str:
    """Read MaterializationRuns for this job and compose a user-facing summary."""
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=procrastinate_job_id,
        )
    ]
    return _compose_failure_summary(runs)


async def _build_agent_for_resume(workspace, user, conversation_id=None):
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
        conversation_id=conversation_id,
    )
    return agent, oauth_tokens


def _resume_langfuse_span(*, thread_job_id: str, thread_id: str, status: str):
    """Open a Langfuse span around the resume ainvoke. No-op when Langfuse is
    not configured so worker boots without LANGFUSE_* env vars stay quiet."""
    if Langfuse is None:
        return contextlib.nullcontext()
    secret_key = getattr(settings, "LANGFUSE_SECRET_KEY", "")
    public_key = getattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    host = getattr(settings, "LANGFUSE_BASE_URL", "")
    if not all([secret_key, public_key, host]):
        return contextlib.nullcontext()
    try:
        client = Langfuse(secret_key=secret_key, public_key=public_key, host=host)
        return client.start_as_current_observation(
            name="resume_thread_after_materialization",
            input={
                "thread_job_id": thread_job_id,
                "thread_id": thread_id,
                "status": status,
            },
        )
    except Exception:
        logger.warning("resume: failed to open Langfuse span", exc_info=True)
        return contextlib.nullcontext()


async def _persist_synthetic_failure_message(thread_job, text: str) -> None:
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
    not a correctness invariant.
    """
    try:
        agent, _ = await _build_agent_for_resume(
            thread_job.thread.workspace,
            thread_job.thread.user,
            conversation_id=str(thread_job.thread.id),
        )
        config = {"configurable": {"thread_id": str(thread_job.thread.id)}}
        await agent.aupdate_state(
            config,
            {"messages": [AIMessage(content=text)]},
        )
    except Exception:
        logger.warning(
            "resume: failed to persist synthetic failure message for tj=%s",
            thread_job.id,
            exc_info=True,
        )


async def _aggregate_materialization_state(procrastinate_job_id: int) -> tuple[str, list[dict]]:
    """Inspect MaterializationRun rows for this job, return (status, per-tenant summary).

    Per-tenant summary entries include per-source detail so the resume prompt
    can tell the agent which sources are queryable and which are unavailable:

    ``{
        "tenant": "...",
        "state": "partial",          # the MaterializationRun state
        "materialized_row_counts": {"users": 100, ...},  # only sources that committed
        "sources": {
            "users":   {"state": "completed", "rows": 100},
            "visits":  {"state": "completed", "rows": 98869},
            "completed_works": {"state": "failed",  "rows": 0,
                                "error": "ConnectionError: 500 ..."},
            "payments": {"state": "skipped", "rows": 0},
            ...
        },
    }``
    """
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
    any_partial = False
    all_completed = True
    for r in runs:
        tenant_id = r.tenant_schema.tenant.external_id
        materialized_row_counts: dict = {}
        sources_detail: dict = {}
        if isinstance(r.result, dict):
            for source, info in (r.result.get("sources") or {}).items():
                if not isinstance(info, dict):
                    continue
                src_state = info.get("state")
                if src_state == "completed" and "rows" in info:
                    materialized_row_counts[source] = info["rows"]
                detail = {"state": src_state, "rows": info.get("rows", 0)}
                if "error" in info:
                    detail["error"] = info["error"]
                # Expose ``cursor_state.last_id`` so the resume prompt can
                # tell the agent "completed_works partially loaded up to
                # id=X — the next materialization will continue from there"
                # (issue #187).
                cursor_state = info.get("cursor_state")
                if isinstance(cursor_state, dict) and isinstance(cursor_state.get("last_id"), int):
                    detail["resume_last_id"] = cursor_state["last_id"]
                sources_detail[source] = detail
        summary.append(
            {
                "tenant": tenant_id,
                "state": r.state,
                "materialized_row_counts": materialized_row_counts,
                "sources": sources_detail,
            }
        )
        if r.state == MaterializationRun.RunState.CANCELLED:
            any_cancelled = True
            all_completed = False
        elif r.state == MaterializationRun.RunState.FAILED:
            any_failed = True
            all_completed = False
        elif r.state == MaterializationRun.RunState.PARTIAL:
            any_partial = True
            all_completed = False
        elif r.state != MaterializationRun.RunState.COMPLETED:
            all_completed = False
    if any_cancelled:
        status = "cancelled"
    elif any_failed:
        status = "failed"
    elif all_completed:
        status = "completed"
    elif any_partial:
        # At least one tenant has some sources committed; the agent can answer
        # questions about what loaded and must disclose what didn't.
        status = "partial"
    else:
        # Runs still in flight (LOADING/TRANSFORMING) — surface as partial so
        # the agent does not falsely claim "all data loaded".
        status = "partial"
    return status, summary


@task(pass_context=True)
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
    # Record started_at on the claim so the reconciler can measure staleness
    # from the RESUME phase, not from created_at (which includes the full
    # materialization + queue time). Without this a healthy long materialization
    # (>10 min) followed by a fresh resume would be falsely flipped to FAILED.
    resume_started_at = timezone.now()
    claimed = await ThreadJob.objects.filter(
        id=tj.id,
        state__in=CLAIMABLE_STATES,
    ).aupdate(state=ThreadJob.State.RUNNING, started_at=resume_started_at)
    if not claimed:
        logger.info("resume: ThreadJob %s already claimed; no-op", thread_job_id)
        return {"status": "already_claimed"}
    # Keep the in-memory instance consistent for any later reads in this task.
    tj.started_at = resume_started_at

    status, summary = await _aggregate_materialization_state(tj.procrastinate_job_id)
    # No in-memory tj.state override: the prior `if tj.state == CANCELLED:
    # status = "cancelled"` block used a snapshot taken *before* the CAS,
    # which produced the wrong message when the user clicked Stop after
    # runs had already finished — the data IS loaded but the agent said
    # "cancelled" and the user's request was abandoned.
    # _aggregate_materialization_state is now the source of truth: if any
    # MaterializationRun is CANCELLED it returns status="cancelled"; if all
    # COMPLETED it returns "completed". A user whose Stop click raced with
    # completion sees the truthful "completed" — their data is intact.

    workspace = tj.thread.workspace
    user = tj.thread.user

    # Multi-tenant workspaces query through a WorkspaceViewSchema that UNION
    # ALLs the per-tenant tables. The per-tenant runs can all complete while
    # build_view_schema fails (a system-side defect), leaving the workspace
    # with NO queryable surface. Detect that here so the agent is told the
    # truth — re-running materialization cannot fix a view-schema build
    # failure, so we must stop it from looping and tell the user a system fix
    # is needed.
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

    if view_schema_failed:
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
    elif status == "no_runs":
        logger.warning(
            "resume: no MaterializationRun rows for ThreadJob %s job_id=%s; "
            "invoking agent with explanation so the user is not left with a spinner",
            thread_job_id,
            tj.procrastinate_job_id,
        )
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization finished without running any "
            f"pipelines. This typically means the workspace's tenants have no "
            f"pipeline configured or no credentials set up. Please tell the user "
            f"what happened and suggest checking the workspace's connection."
        )
    elif status == "partial":
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization completed with PARTIAL data "
            f"(some sources loaded, others failed or were skipped). Answer what "
            f"you can from the available data and tell the user which sources are "
            f"unavailable. Do NOT claim that data is loaded for sources marked "
            f"failed or skipped. A source with state=in_progress or state=failed "
            f"and a non-null resume_last_id has partially-loaded rows that the "
            f"next materialization will continue from — do NOT query its table "
            f"as if it were complete. Per-tenant: {summary}"
        )
    elif status == "failed":
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization FAILED — every source failed, "
            f"so there is NO loaded data for this workspace. Do NOT claim the "
            f"materialization completed and do NOT query the workspace's tables as "
            f"if data were present; there is nothing there. Tell the user plainly "
            f"that the data load failed (this is commonly caused by expired or "
            f"revoked credentials), summarize the per-source errors below, and "
            f"suggest checking the workspace's connection before retrying. Do NOT "
            f"silently re-run materialization. Per-tenant: {summary}"
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
        body = (
            f"{SYSTEM_RESUME_MARKER} Materialization just completed "
            f"(status={status}). Please continue with the user's original request "
            f"using the now-loaded data. Per-tenant: {summary}"
        )

    timeout_s = getattr(settings, "AGENT_RESUME_TIMEOUT_S", 120)
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
        agent, oauth_tokens = await _build_agent_for_resume(
            workspace, user, conversation_id=str(tj.thread.id)
        )
        input_state = {
            "messages": [HumanMessage(content=body)],
            "workspace_id": str(workspace.id),
            "user_id": str(user.id),
            "user_role": "analyst",
            "thread_id": str(tj.thread.id),
        }
        config = {
            "configurable": {"thread_id": str(tj.thread.id)},
            "recursion_limit": settings.AGENT_RESUME_RECURSION_LIMIT,
            "oauth_tokens": oauth_tokens,
        }
        with _resume_langfuse_span(
            thread_job_id=thread_job_id,
            thread_id=str(tj.thread.id),
            status=status,
        ):
            await asyncio.wait_for(
                agent.ainvoke(input_state, config),
                timeout=timeout_s,
            )
    except TimeoutError:
        elapsed = time.monotonic() - start
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
        await _persist_synthetic_failure_message(tj, RESUME_TIMEOUT_MESSAGE)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
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
        await _persist_synthetic_failure_message(tj, RESUME_EXCEPTION_MESSAGE)
        await ThreadJob.objects.filter(id=tj.id).aupdate(
            state=ThreadJob.State.FAILED,
            completed_at=timezone.now(),
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
    sentry_sdk.add_breadcrumb(
        category="resume",
        message="ainvoke_complete",
        data={"thread_job_id": str(tj.id), "elapsed_s": time.monotonic() - start},
    )

    # Bump Thread.updated_at so the sidebar's "newer than last_viewed" check
    # fires the green-dot indicator after a successful background resume.
    # Isolated try/except: a DB failure here must not contaminate the
    # success path (the agent message was already persisted via ainvoke).
    try:
        await Thread.objects.filter(id=tj.thread_id).aupdate(updated_at=timezone.now())
    except Exception:
        logger.warning(
            "resume: Thread.updated_at bump failed for thread %s; green-dot indicator may not fire",
            tj.thread_id,
            exc_info=True,
        )

    terminal = (
        ThreadJob.State.CANCELLED
        if status == "cancelled"
        # A view-schema build failure leaves the workspace with no queryable
        # surface even when every per-tenant run completed, so it is not a
        # success — flip to FAILED so the spinner clears into an error state.
        else (
            ThreadJob.State.FAILED
            if (status in ("failed", "partial", "no_runs") or view_schema_failed)
            else ThreadJob.State.COMPLETED
        )
    )
    error_summary = ""
    if terminal == ThreadJob.State.FAILED:
        if view_schema_failed:
            error_summary = (
                "Per-tenant data loaded, but the workspace query layer (view "
                f"schema) failed to build: {view_schema_error}. A system-side "
                "fix is required — re-running materialization will not help."
            )
        elif status == "no_runs":
            error_summary = (
                "Materialization finished without running any pipelines. "
                "Check that the workspace's tenants have credentials configured."
            )
        else:
            error_summary = await _build_failure_summary_for_job(tj.procrastinate_job_id)
            if not error_summary:
                error_summary = "Materialization did not complete successfully."
    # CAS-scoped to state=RUNNING (the value we set when claiming the job).
    # If a concurrent cancel landed during agent.ainvoke (which can take 30s+),
    # the row is already CANCELLED and we must NOT clobber it back to a
    # success terminal. The filter returns zero rows; we then re-read the
    # actual persisted state so the return value reflects reality, not the
    # value we *would have* written.
    updated = await ThreadJob.objects.filter(
        id=tj.id,
        state=ThreadJob.State.RUNNING,
    ).aupdate(
        state=terminal,
        completed_at=timezone.now(),
        error_summary=error_summary,
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
