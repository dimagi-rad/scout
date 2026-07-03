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
from apps.agents.mcp_client import get_mcp_tools
from apps.chat.checkpointer import ensure_checkpointer
from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.chat.models import Thread, ThreadJob
from apps.transformations.models import TransformationRunStatus
from apps.users.models import TenantMembership
from apps.users.services.credential_resolver import (
    CredentialResolutionError,
    aresolve_credential,
)
from apps.workspaces.models import (
    VIEW_SCHEMA_CASCADE_TEARDOWN_ERROR,
    VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER,
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


# Substrings that mark a per-source failure as an expired/revoked-credential
# problem so the user is told to reconnect rather than just "check the
# connection" (arch #252, finding 14#4). Loader auth errors carry both the
# class-name suffix and the actionable "reconnect your ... account" guidance;
# an HTTP 401 anywhere on the seam is the underlying signal.
_AUTH_FAILURE_MARKERS = ("AuthError", "reconnect your", "HTTP 401")
_REAUTH_GUIDANCE = (
    "This looks like an expired or revoked sign-in — reconnect the affected "
    "account (Settings → Connections) and re-run materialization."
)


def _looks_like_auth_failure(error: str | None) -> bool:
    """True when a per-source error string reads as a credential/401 failure."""
    if not error:
        return False
    return any(marker in error for marker in _AUTH_FAILURE_MARKERS)


def _no_pipeline_error(registry, provider: str) -> str:
    """Build the 'no pipeline for provider' error, distinguishing cause (07#7).

    An unconfigured provider and a pipeline YAML that failed to parse used to
    share one message that wrongly pointed at workspace config; when the registry
    recorded load errors, say so explicitly so blame lands on the deploy.
    """
    load_errors = registry.load_errors
    if load_errors:
        return (
            f"No pipeline available for provider '{provider}': "
            f"{len(load_errors)} pipeline definition(s) failed to load "
            f"({', '.join(sorted(load_errors))}). This is a deploy/config error, "
            "not a workspace setting — check the pipeline YAML files."
        )
    return f"No pipeline configured for provider '{provider}'"


def _compose_failure_summary(runs: list[MaterializationRun]) -> str:
    """Compose a human-readable failure summary for ``ThreadJob.error_summary``.

    Reads the per-source state map in ``run.result["sources"]`` (post-#198 shape).
    Returns "" when there is nothing to summarize — callers fall back to a generic
    message.
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
        # No per-source detail (failure before any source ran) — surface run state.
        states = sorted({r.state for r in runs})
        return f"Materialization {'/'.join(states)}."
    summary = ". ".join(parts) + "."
    if any(_looks_like_auth_failure(err) for _, err in failed_sources):
        summary += " " + _REAUTH_GUIDANCE
    return summary


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

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.create_physical_schema, new_schema)
    except Exception:
        logger.exception("Failed to create schema '%s'", new_schema.schema_name)
        new_schema.state = SchemaState.FAILED
        await new_schema.asave(update_fields=["state"])
        return {"error": "Failed to create schema"}

    # Async job: must use the async resolver — the sync one raises
    # SynchronousOnlyOperation here.
    try:
        credential = await aresolve_credential(membership)
    except CredentialResolutionError as e:
        # Surface the distinct message + code so the user is told to re-connect
        # rather than the generic "No credential available" (arch #245 finding 07#3).
        await _drop_schema_and_fail(new_schema)
        return {"error": e.message, "error_code": e.code}
    if credential is None:
        await _drop_schema_and_fail(new_schema)
        return {"error": "No credential available"}

    try:
        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}
        pipeline_name = provider_pipeline_map.get(membership.tenant.provider)
        if pipeline_name is None:
            await _drop_schema_and_fail(new_schema)
            return {
                "error": _no_pipeline_error(registry, membership.tenant.provider),
            }
        pipeline_config = registry.get(pipeline_name)
        # target_schema forces the load into the new "_r" schema; without it
        # run_pipeline re-resolves the old active base schema and data lands there.
        await _to_thread_fresh_db(
            run_pipeline, membership, credential, pipeline_config, target_schema=new_schema
        )
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        await _drop_schema_and_fail(new_schema)
        return {"error": "Materialization failed"}

    # Reset last_accessed_at so the fresh schema starts with a clean inactivity
    # TTL — otherwise expire_inactive_schemas could drop it before first use.
    new_schema.state = SchemaState.ACTIVE
    new_schema.last_accessed_at = timezone.now()
    await new_schema.asave(update_fields=["state", "last_accessed_at"])

    # The tenant data schema is SHARED across workspaces; this refresh swapped in a
    # NEW physical schema. Dependent multi-tenant view schemas still point at the OLD
    # (about-to-be-torn-down) schema, so rebuild them against the new ACTIVE schema —
    # mirroring the sibling rebuild materialize_workspace performs (PR #230).
    await _rebuild_dependent_view_schemas([new_schema.tenant_id])

    # Delay teardown of previously active schemas so in-flight queries can drain.
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


async def materialize_workspace_core(
    workspace_id: str,
    user_id: str = "",
    job_id: int | None = None,
) -> dict:
    """Run materialization for all tenants in a workspace and rebuild view schemas.

    Returns a per-tenant summary. Does NOT defer any chat-resume task — the
    interactive chat path uses the ``materialize_workspace`` Procrastinate task
    (which wraps this and defers ``resume_thread_after_materialization``);
    headless callers (e.g. the recipe runner's blocking materialize tool) call
    this directly and block on the return value.

    Writes progress to ``MaterializationRun.progress`` (keyed by ``job_id``)
    after each page so the MCP polling loop can surface real-time status. The
    ``progress_updater`` closure also acts as the cancellation checkpoint: it
    re-reads ``MaterializationRun.state`` and raises ``MaterializationCancelled``
    when the run has been marked CANCELLED, triggering a transaction rollback.
    """
    tenant_results: list[dict] = []

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
                    "error": _no_pipeline_error(registry, tm.tenant.provider),
                }
            )
            continue

        try:
            credential = await aresolve_credential(tm)
        except CredentialResolutionError as e:
            # Actionable failure (e.g. token scoped to a different team) —
            # surface a distinct message + code so the user knows to
            # re-connect, not the generic "No credential configured"
            # (arch #245 finding 07#3).
            tenant_results.append(
                {
                    "tenant": tenant_id,
                    "success": False,
                    "error": e.message,
                    "error_code": e.code,
                }
            )
            continue
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
            break
        except ConnectExportError as e:
            # Capture the sentry-trace header so support can correlate with
            # Connect's Sentry in one hop. set_tag is a no-op without a DSN.
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

    # A partial/cancelled multi-tenant run DROP-CASCADEs some namespaced views,
    # leaving the workspace's own view schema ACTIVE-but-missing. Rebuild it
    # unconditionally (not only on full success) before the resume fires (arch #255 03#1).
    view_schema_outcome: dict | None = None
    workspace_tenant_count = await workspace.workspace_tenants.acount()
    if workspace_tenant_count > 1:
        try:
            await _to_thread_fresh_db(SchemaManager().build_view_schema, workspace)
            view_schema_outcome = {"ok": True, "error": None}
        except Exception as exc:
            # Don't re-raise — the resume task must still fire. The failure is
            # recorded on the WorkspaceViewSchema row (state=FAILED, last_error),
            # which the resume task reads directly.
            logger.exception(
                "Post-materialization view schema rebuild failed for workspace %s",
                workspace_id,
            )
            view_schema_outcome = {"ok": False, "error": str(exc)[:500]}

    # Tenant data schemas (t_<id>) are SHARED. Re-materializing drops & recreates
    # raw_* tables, cascade-dropping the namespaced views in every OTHER workspace's
    # view schema (leaving them ACTIVE but empty). Rebuild each sibling multi-tenant
    # workspace's views against the new tables.
    await _rebuild_dependent_view_schemas(
        [tm.tenant_id for tm in memberships],
        exclude_workspace_id=str(workspace.id),
    )

    return {
        "tenants": tenant_results,
        "all_succeeded": all_succeeded,
        "view_schema": view_schema_outcome,
    }


async def _await_in_progress_materializations(
    workspace_id: str, *, poll_interval: float = 2.0, max_wait_seconds: float = 1800.0
) -> None:
    """Block until no materialization is ACTIVE for this workspace's tenants.

    Headless callers (recipes) call this before starting their own run so they
    do not execute a parallel materialization against the same tenant schemas —
    the pipeline drops & recreates ``raw_*`` tables, so concurrent runs corrupt
    each other. Best-effort: on timeout, log and return so the caller proceeds.
    """
    tenant_ids = [
        wt.tenant_id async for wt in WorkspaceTenant.objects.filter(workspace_id=workspace_id)
    ]
    if not tenant_ids:
        return
    # Poll cross-process MaterializationRun state (another worker owns the run,
    # so no in-process Event to await). Bounded to keep a ceiling on the wait.
    max_polls = max(1, int(max_wait_seconds / poll_interval))
    for _ in range(max_polls):
        in_progress = await MaterializationRun.objects.filter(
            tenant_schema__tenant_id__in=tenant_ids,
            state__in=list(MaterializationRun.ACTIVE_STATES),
        ).aexists()
        if not in_progress:
            return
        await asyncio.sleep(poll_interval)
    logger.warning(
        "materialize_workspace_blocking: still waiting on an in-progress materialization "
        "of workspace %s after ~%.0fs; proceeding",
        workspace_id,
        max_wait_seconds,
    )


async def materialize_workspace_blocking(
    workspace_id: str, user_id: str = "", job_id: int | None = None
) -> dict:
    """Ensure the workspace is materialized, blocking until done.

    Unlike the bare ``materialize_workspace_core``, this first WAITS for any
    materialization already in progress for the workspace's tenants to finish,
    then runs a fresh one — so a headless recipe never starts a second, parallel
    materialization against the same tenant schema (which the interactive path
    avoids by telling the agent not to). Returns the core summary shape.
    """
    await _await_in_progress_materializations(workspace_id)
    return await materialize_workspace_core(workspace_id, user_id, job_id)


@task(pass_context=True)
async def materialize_workspace(
    context,
    workspace_id: str,
    user_id: str = "",
) -> dict:
    """Procrastinate task: run materialization for a workspace, then ALWAYS
    defer the chat-resume task so an interactive user is never left with a
    phantom spinner — even on early-return paths (workspace missing, no
    memberships) where the per-tenant loop never executed.

    The actual work lives in ``materialize_workspace_core`` so headless callers
    (recipes) can reuse it without the fire-and-resume machinery.
    """
    job_id = context.job.id
    try:
        return await materialize_workspace_core(workspace_id, user_id, job_id)
    finally:
        await _defer_resume_for_job(job_id)


async def _defer_resume_for_job(job_id: int) -> None:
    """Find the ThreadJob bound to ``job_id`` and defer the resume task.

    MCP commits the ThreadJob row *after* defer_async returns the job id, so under
    load the worker may finish before the row is visible — hedge with a bounded
    backoff (~3.75s). If still not visible, the janitor catches up eventually.

    TODO: cleaner fix is for MCP to write a placeholder ThreadJob before
    defer_async, then patch in procrastinate_job_id (needs a nullable migration).
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


async def _to_thread_fresh_db(func, /, *args, **kwargs):
    """Run a sync ORM-touching callable on a to_thread pool thread, closing
    stale/dead DB connections on that SAME thread first (arch #253, 08#0).

    Pool threads are reused across jobs and the worker's connection cleanup only
    reaches the async-ORM thread, so a connection that died since this pool
    thread's last run would otherwise poison the call. The cleanup runs inside
    the threaded callable so it never touches the caller thread's connection.
    """

    def _guarded():
        close_old_connections()
        return func(*args, **kwargs)

    return await asyncio.to_thread(_guarded)


def _run_pipeline_with_progress(
    tenant_membership,
    credential: dict,
    pipeline_config,
    job_id: int,
) -> dict:
    """Synchronous entry point invoked under ``asyncio.to_thread``.

    Builds the ``progress_updater`` closure (mirrors progress to the DB and
    surfaces cancellation), then runs the pipeline.
    """
    # Pool thread's connection is unreachable by the worker's async-ORM cleanup
    # and may have died since the last job here — close it so the first use reopens.
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

    # Log last_accessed_at BEFORE flipping — teardown/provision later overwrites
    # it, and that timestamp was the forensic input the 2026-06-10 incident review
    # could not recover (arch #257, finding 08#9).
    async for schema in TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        logger.info(
            "expire_inactive_schemas: marking tenant schema %s (%s) for teardown — "
            "last_accessed_at=%s cutoff=%s ttl_hours=%s",
            schema.id,
            schema.schema_name,
            schema.last_accessed_at.isoformat() if schema.last_accessed_at else None,
            cutoff.isoformat(),
            settings.SCHEMA_TTL_HOURS,
        )
        schema.state = SchemaState.TEARDOWN
        await schema.asave(update_fields=["state"])
        await teardown_schema.defer_async(schema_id=str(schema.id))

    # Expire stale view schemas (same forensic logging as tenant schemas above).
    async for vs in WorkspaceViewSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        logger.info(
            "expire_inactive_schemas: marking view schema %s (%s) for teardown — "
            "last_accessed_at=%s cutoff=%s ttl_hours=%s",
            vs.id,
            vs.schema_name,
            vs.last_accessed_at.isoformat() if vs.last_accessed_at else None,
            cutoff.isoformat(),
            settings.SCHEMA_TTL_HOURS,
        )
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
        vs = await _to_thread_fresh_db(manager.build_view_schema, workspace)
    except Exception:
        # build_view_schema owns the row state (marks it FAILED on any failure), so
        # don't re-write state here and risk clobbering a concurrent transition —
        # e.g. TEARDOWN set by expire_inactive_schemas (arch #255 03#2).
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

    # State CAS (arch #237, finding 03#0): abort if no longer TEARDOWN — a
    # rebuild → ACTIVE after queueing means the physical schema is live again.
    if vs.state != SchemaState.TEARDOWN:
        logger.info(
            "teardown_view_schema_task: view schema %s is %s (not TEARDOWN) — "
            "aborting drop; the row was likely reactivated after teardown was queued",
            vs.id,
            vs.state,
        )
        return

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown_view_schema, vs)
    except Exception:
        logger.exception("Failed to drop view schema '%s'", vs.schema_name)
        vs.state = SchemaState.ACTIVE
        await vs.asave(update_fields=["state"])
        raise

    # Destructive op must leave a trace (arch #257, finding 08#9).
    logger.info(
        "teardown_view_schema_task: DROP SCHEMA CASCADE succeeded for view schema %s (%s) — "
        "last_accessed_at=%s",
        vs.id,
        vs.schema_name,
        vs.last_accessed_at.isoformat() if vs.last_accessed_at else None,
    )

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

    # State CAS (arch #237, finding 03#0): provision() resurrects EXPIRED/TEARDOWN
    # rows to ACTIVE (2026-06-10 incident-b fix). If that raced ahead of this queued
    # teardown the re-provisioned data must be preserved — abort unless still TEARDOWN.
    if schema.state != SchemaState.TEARDOWN:
        logger.info(
            "teardown_schema: schema %s is %s (not TEARDOWN) — aborting drop; "
            "the row was likely resurrected by provision() after teardown was queued",
            schema.id,
            schema.state,
        )
        return

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown, schema)
    except Exception:
        # teardown() only raises when DROP SCHEMA itself fails, so the physical
        # schema (and its tables) still exists — revert to ACTIVE rather than
        # stranding it in TEARDOWN, and leave the data-bearing runs terminal so
        # the catalog keeps surfacing them.
        schema.state = SchemaState.ACTIVE
        await schema.asave(update_fields=["state"])
        raise

    # Destructive op must leave a forensic trace (arch #257, finding 08#9).
    logger.info(
        "teardown_schema: DROP SCHEMA CASCADE succeeded for tenant schema %s (%s) — "
        "last_accessed_at=%s",
        schema.id,
        schema.schema_name,
        schema.last_accessed_at.isoformat() if schema.last_accessed_at else None,
    )

    # Tables are now dropped: flip data-bearing runs to STALE so
    # pipeline_list_tables stops returning ghosts. Done after the DROP succeeds
    # (not at TEARDOWN-flip) so a failed DROP never strands intact data as invisible.
    await MaterializationRun.objects.filter(
        tenant_schema=schema,
        state__in=[
            MaterializationRun.RunState.COMPLETED,
            MaterializationRun.RunState.PARTIAL,
        ],
    ).aupdate(state=MaterializationRun.RunState.STALE)

    # The DROP CASCADE just cascade-dropped the namespaced views in every dependent
    # multi-tenant view schema; _reconcile rebuilds (if the tenant has a surviving
    # ACTIVE schema) or fails them (pure TTL expiry left no data).
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

    If the tenant has another ACTIVE schema (refresh path), rebuild the views
    against it; if none survives (pure TTL expiry), flip them FAILED so the catalog
    reports the truth instead of serving an empty view.

    ``exclude(id=schema.id)`` matters: a direct caller may pass an ACTIVE row, so
    excluding it makes the "another ACTIVE schema?" check correct either way.
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
        # Log the count so a cascade that silently degrades N workspaces is
        # visible (arch #257, finding 08#9).
        failed_count = await _fail_dependent_view_schemas(schema.tenant_id)
        if failed_count:
            logger.warning(
                "teardown_schema: %d dependent multi-tenant view schema(s) flipped to "
                "FAILED after tenant schema %s (%s) was dropped (their namespaced views "
                "were cascade-dropped and the tenant has no surviving data)",
                failed_count,
                schema.id,
                schema.schema_name,
            )


async def _fail_dependent_view_schemas(tenant_id) -> int:
    """Flip every ACTIVE WorkspaceViewSchema depending on ``tenant_id`` to FAILED.

    Only ACTIVE rows — TEARDOWN/FAILED/EXPIRED must not be clobbered out of their
    lifecycle state. Returns the number of rows flipped.
    """
    dependent_workspace_ids = (
        Workspace.objects.filter(workspace_tenants__tenant_id=tenant_id)
        .annotate(num_tenants=_multi_tenant_count_subquery())
        .filter(num_tenants__gte=2)
        .values("id")
    )
    # Truthful last_error for the cascade (07#9): the marker lets the resume logic
    # advise a re-run, instead of the generic "system-side fix required" — wrong
    # here, since re-materializing the torn-down tenant IS the fix.
    return await WorkspaceViewSchema.objects.filter(
        workspace_id__in=dependent_workspace_ids,
        state=SchemaState.ACTIVE,
    ).aupdate(
        state=SchemaState.FAILED,
        last_error=VIEW_SCHEMA_CASCADE_TEARDOWN_ERROR,
    )


STALE_JOB_THRESHOLD = timedelta(minutes=10)


def _staleness_anchor(tj: ThreadJob):
    """Timestamp from which a ThreadJob's staleness is measured.

    RUNNING jobs measure from the RESUME phase (``started_at``); created_at
    includes the full materialization, so a long materialization + fresh resume
    would otherwise look instantly stale (finding 02#9). Everything else falls
    back to ``created_at`` so an unclaimed job still ages out.
    """
    if tj.state == ThreadJob.State.RUNNING and tj.started_at is not None:
        return tj.started_at
    return tj.created_at


def _stale_active_jobs_q(cutoff) -> Q:
    """Predicate matching active ThreadJobs whose staleness anchor is older than
    ``cutoff`` (see :func:`_staleness_anchor`).

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
        anchor = _staleness_anchor(tj)
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
        await _persist_synthetic_failure_message(tj, RESUME_STUCK_RUNNING_MESSAGE)
        return "failed"
    if status in _PROCRASTINATE_FAILED_STATUSES:
        # The task itself failed/was aborted/cancelled — no result for a resume to
        # narrate, so flip straight to FAILED instead of deferring a resume with
        # nothing to say.
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
    # defer a fresh resume; the resume task flips the state.
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


async def _fail_thread_jobs_for_dead_materialization(procrastinate_job_id: int) -> None:
    """Fail any active ThreadJob(s) owning a dead materialization job, with a
    truthful summary + synthetic chat message so the UI spinner clears.

    The ThreadJob janitor can't do this for a hard worker death: it returns None
    for a 'doing' zombie job (correct per-tick, permanent for zombies).
    """
    summary = (
        await _build_failure_summary_for_job(procrastinate_job_id) or MATERIALIZATION_FAILED_MESSAGE
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
            error_summary=summary,
        )
        if updated:
            await _persist_synthetic_failure_message(tj, MATERIALIZATION_FAILED_MESSAGE)


async def _fail_zombie_materialization_run(run: MaterializationRun, reason: str) -> bool:
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


@app.periodic(cron="*/15 * * * *")
@task
async def reconcile_stale_materialization_runs(timestamp: int = 0) -> dict:
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
        if await _fail_zombie_materialization_run(run, reason):
            failed += 1
    return {"failed": failed}


# procrastinate_jobs / procrastinate_events grow unbounded otherwise: ~144 janitor
# jobs/day plus every materialization/teardown/rebuild/resume. Keep finalized jobs
# for a week (forensics + idempotency headroom) then prune (arch #255, 10#0).
JOB_RETENTION_HOURS = 24 * 7


@app.periodic(cron="17 3 * * *")
@task
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
        agent = await _build_agent_for_resume(
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
        transform_error: str | None = None
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
                # Expose cursor_state.last_id so the resume prompt can tell the
                # agent where a partial load will continue from (issue #187).
                cursor_state = info.get("cursor_state")
                if isinstance(cursor_state, dict) and isinstance(cursor_state.get("last_id"), int):
                    detail["resume_last_id"] = cursor_state["last_id"]
                sources_detail[source] = detail
            # Surface a failed transform phase (issue #241, 04#4): run state stays
            # COMPLETED (transform failures are isolated from the raw load), but
            # staging/derived tables are stale and the agent must disclose that.
            transforms = r.result.get("transforms")
            if isinstance(transforms, dict):
                if transforms.get("status") == TransformationRunStatus.FAILED:
                    transform_error = transforms.get("error") or "transform phase failed"
                elif transforms.get("error"):
                    transform_error = transforms["error"]
        tenant_summary = {
            "tenant": tenant_id,
            "state": r.state,
            "materialized_row_counts": materialized_row_counts,
            "sources": sources_detail,
        }
        if transform_error:
            tenant_summary["transform_error"] = transform_error
        summary.append(tenant_summary)
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
        status = "partial"
    else:
        # Runs still in flight (LOADING/TRANSFORMING) — partial so the agent
        # does not falsely claim "all data loaded".
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

    # _aggregate_materialization_state is the source of truth for status (not a
    # pre-CAS tj.state snapshot, which mislabelled a Stop-click that raced with
    # completion as "cancelled" when the data had actually loaded).
    status, summary = await _aggregate_materialization_state(tj.procrastinate_job_id)
    auth_failure = any(
        _looks_like_auth_failure(str(src.get("error")))
        for tenant in summary
        for src in (tenant.get("sources") or {}).values()
    )
    reauth_line = (
        f" At least one source failed authentication: {_REAUTH_GUIDANCE} Tell the "
        f"user explicitly to reconnect the affected account."
        if auth_failure
        else ""
    )

    workspace = tj.thread.workspace
    user = tj.thread.user

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

    if view_schema_failed:
        if VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER in view_schema_error:
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
            f"as if it were complete.{reauth_line} Per-tenant: {summary}"
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
            f"silently re-run materialization.{reauth_line} Per-tenant: {summary}"
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
        agent = await _build_agent_for_resume(workspace, user, conversation_id=str(tj.thread.id))
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

    terminal = (
        ThreadJob.State.CANCELLED
        if status == "cancelled"
        # A view-schema build failure leaves no queryable surface even with every
        # run COMPLETED, so it's FAILED, not success.
        else (
            ThreadJob.State.FAILED
            if (status in ("failed", "partial", "no_runs") or view_schema_failed)
            else ThreadJob.State.COMPLETED
        )
    )
    error_summary = ""
    if terminal == ThreadJob.State.FAILED:
        if view_schema_failed and VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER in view_schema_error:
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
        elif status == "no_runs":
            error_summary = (
                "Materialization finished without running any pipelines. "
                "Check that the workspace's tenants have credentials configured."
            )
        else:
            error_summary = await _build_failure_summary_for_job(tj.procrastinate_job_id)
            if not error_summary:
                error_summary = "Materialization did not complete successfully."
    # CAS-scoped to state=RUNNING: a concurrent cancel during ainvoke leaves the
    # row CANCELLED, so this matches zero rows rather than clobbering it back to a
    # success terminal; we then re-read the actual persisted state below.
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
