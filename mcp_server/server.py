"""
Scout MCP Server.

Database access layer for the Scout agent, exposed via the Model Context
Protocol. Runs as a standalone process but uses Django ORM to load project
configuration and database credentials.

Tools receive a workspace_id (injected server-side by the agent graph)
to route queries to the correct schema. All responses use a consistent
envelope format.

Usage:
    # stdio transport (for local clients)
    python -m mcp_server

    # HTTP transport (for networked clients)
    python -m mcp_server --transport streamable-http
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime

from django.core.exceptions import ValidationError as _ValidationError
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from procrastinate.contrib.django.procrastinate_app import current_app as _procrastinate_app
from procrastinate.jobs import Status as _ProcrastinateStatus

from apps.transformations.services.lineage import aget_lineage_chain
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantMetadata,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.tasks import materialize_workspace
from mcp_server.context import load_workspace_context
from mcp_server.envelope import (
    INTERNAL_ERROR,
    NOT_FOUND,
    VALIDATION_ERROR,
    error_response,
    success_response,
    tool_context,
)
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_get_metadata,
    pipeline_list_tables,
    workspace_list_tables,
)
from mcp_server.services.query import execute_query

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


async def _resolve_mcp_context(workspace_id: str):
    """Load a QueryContext for the workspace."""
    if not workspace_id:
        raise ValueError("workspace_id is required")
    return await load_workspace_context(workspace_id)


async def _resolve_pipeline_config(ts, last_run):
    """Pick the right PipelineConfig for a TenantSchema.

    Prefers the pipeline of the last completed materialization run; falls back
    to the pipeline registered for the tenant's provider; falls back to
    ``commcare_sync`` as a last resort to preserve historical behavior.

    ``ts`` may be ``None`` when the workspace is multi-tenant and the caller is
    looking at a workspace view schema (``ws_*``) rather than a tenant schema.
    In that case we can't infer a tenant-specific pipeline, so just fall back
    to commcare_sync for pipeline-derived metadata (per-tenant routing happens
    at load time, not at metadata-describe time).
    """
    registry = get_registry()
    if last_run:
        cfg = registry.get(last_run.pipeline)
        if cfg:
            return cfg
    if ts is not None:
        tenant = await Tenant.objects.aget(id=ts.tenant_id)
        cfg = registry.get_by_provider(tenant.provider)
        if cfg:
            return cfg
    return registry.get("commcare_sync")


# --- Tools ---


@mcp.tool()
async def list_tables(workspace_id: str = "") -> dict:
    """List all tables in the workspace's database schema.

    Returns table names, types, descriptions, row counts, and materialization timestamps.
    Returns an empty list if no materialization run has completed yet.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("list_tables", workspace_id) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        # For multi-tenant workspaces, the context points at a WorkspaceViewSchema
        # (namespaced views). Use information_schema directly instead of MaterializationRun.
        if workspace_id:
            is_view_schema = await WorkspaceViewSchema.objects.filter(
                schema_name=ctx.schema_name, state=SchemaState.ACTIVE
            ).aexists()
            if is_view_schema:
                tables = await workspace_list_tables(ctx)
                tc["result"] = success_response(
                    {"tables": tables, "note": None},
                    schema=ctx.schema_name,
                    timing_ms=tc["timer"].elapsed_ms,
                )
                return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"tables": [], "note": None},
                schema=ctx.schema_name,
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema=ts,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        pipeline_config = await _resolve_pipeline_config(ts, last_run)

        tables = await pipeline_list_tables(ts, pipeline_config)

        note = (
            "No completed materialization run found. Run run_materialization to load data."
            if not tables
            else None
        )
        tc["result"] = success_response(
            {"tables": tables, "note": note},
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def describe_table(table_name: str, workspace_id: str = "") -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default, description) and a table description.
    JSONB columns are annotated with summaries from the CommCare discover phase when available.

    Args:
        table_name: Name of the table to describe.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("describe_table", workspace_id, table_name=table_name) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()

        last_run = None
        tenant_metadata = None
        if ts is not None:
            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=ts,
                    state=MaterializationRun.RunState.COMPLETED,
                )
                .order_by("-completed_at")
                .afirst()
            )
            tenant_metadata = await TenantMetadata.objects.filter(
                tenant_membership__tenant_id=ts.tenant_id
            ).afirst()

        pipeline_config = await _resolve_pipeline_config(ts, last_run)

        table = await pipeline_describe_table(table_name, ctx, tenant_metadata, pipeline_config)
        if table is None:
            tc["result"] = error_response(
                NOT_FOUND, f"Table '{table_name}' not found in schema '{ctx.schema_name}'"
            )
            return tc["result"]

        tc["result"] = success_response(
            table,
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_metadata(workspace_id: str = "") -> dict:
    """Get a complete metadata snapshot for the workspace's database.

    Returns all tables with their columns, descriptions, and table relationships
    defined by the materialization pipeline.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("get_metadata", workspace_id) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()
        if ts is None:
            tc["result"] = success_response(
                {"schema": ctx.schema_name, "table_count": 0, "tables": {}, "relationships": []},
                schema=ctx.schema_name,
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema=ts,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        pipeline_config = await _resolve_pipeline_config(ts, last_run)

        tenant_metadata = await TenantMetadata.objects.filter(
            tenant_membership__tenant_id=ts.tenant_id
        ).afirst()

        metadata = await pipeline_get_metadata(ts, ctx, tenant_metadata, pipeline_config)

        tc["result"] = success_response(
            {
                "schema": ctx.schema_name,
                "table_count": len(metadata["tables"]),
                "tables": metadata["tables"],
                "relationships": metadata["relationships"],
            },
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_lineage(model_name: str, workspace_id: str = "") -> dict:
    """Get the transformation lineage for a model.

    Returns the chain of transformations from the given model back to the raw
    source data, showing what each step does and why. Use this when the user
    asks about data provenance, how a table was created, or what cleaning
    or transformations were applied to the data.

    Args:
        model_name: Name of the model to trace lineage for.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("get_lineage", workspace_id, model_name=model_name) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        try:
            workspace = await Workspace.objects.aget(id=workspace_id)
        except Workspace.DoesNotExist:
            tc["result"] = error_response(NOT_FOUND, f"Workspace '{workspace_id}' not found")
            return tc["result"]

        tenant_ids = [t.id async for t in workspace.tenants.all()]

        chain = await aget_lineage_chain(
            model_name, tenant_ids=tenant_ids, workspace_id=workspace_id
        )

        if not chain:
            tc["result"] = error_response(
                NOT_FOUND, f"No transformation asset named '{model_name}' found"
            )
            return tc["result"]

        tc["result"] = success_response(
            {"model": model_name, "lineage": chain},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def query(sql: str, workspace_id: str = "") -> dict:
    """Execute a read-only SQL query against the workspace's database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        sql: A SQL SELECT query to execute.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("query", workspace_id, sql=sql) as tc:
        try:
            ctx = await _resolve_mcp_context(workspace_id)
        except (ValueError, _ValidationError) as e:
            tc["result"] = error_response(VALIDATION_ERROR, str(e))
            return tc["result"]

        result = await execute_query(ctx, sql)

        # execute_query returns an error envelope on failure
        if not result.get("success", True):
            tc["result"] = result
            return tc["result"]

        warnings = []
        if result.get("truncated"):
            warnings.append(f"Results truncated to {ctx.max_rows_per_query} rows")

        tc["result"] = success_response(
            {
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result.get("truncated", False),
                "sql_executed": result.get("sql_executed", ""),
                "tables_accessed": result.get("tables_accessed", []),
            },
            schema=ctx.schema_name,
            timing_ms=tc["timer"].elapsed_ms,
            warnings=warnings or None,
        )
        return tc["result"]


@mcp.tool()
async def list_pipelines() -> dict:
    """List available materialization pipelines and their descriptions.

    Returns the registry of pipelines that can be run via run_materialization.
    Each entry includes the pipeline name, description, provider, sources, and DBT models.
    """
    async with tool_context("list_pipelines", "") as tc:
        registry = get_registry()
        pipelines = [
            {
                "name": p.name,
                "description": p.description,
                "provider": p.provider,
                "version": p.version,
                "sources": [{"name": s.name, "description": s.description} for s in p.sources],
                "has_metadata_discovery": p.has_metadata_discovery,
                "dbt_models": p.dbt_models,
            }
            for p in registry.list()
        ]
        tc["result"] = success_response(
            {"pipelines": pipelines},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_materialization_status(run_id: str) -> dict:
    """Retrieve the status of a materialization run by ID.

    Primarily a fallback for reconnection scenarios — live progress is delivered
    via MCP progress notifications during an active run_materialization call.

    Args:
        run_id: UUID of the MaterializationRun to look up.
    """
    async with tool_context("get_materialization_status", run_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related("tenant_schema__tenant").aget(
                id=run_id
            )
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        tenant_id = run.tenant_schema.tenant.external_id
        schema = run.tenant_schema.schema_name

        tc["result"] = success_response(
            {
                "run_id": str(run.id),
                "pipeline": run.pipeline,
                "state": run.state,
                "result": run.result,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "tenant_id": tenant_id,
            },
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def cancel_materialization(run_id: str) -> dict:
    """Cancel a running materialization pipeline.

    Marks the run as failed in the database. This is a best-effort cancellation —
    in-flight loader operations may not terminate immediately. Full subprocess
    cancellation is a future feature.

    Args:
        run_id: UUID of the MaterializationRun to cancel.
    """
    async with tool_context("cancel_materialization", run_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related("tenant_schema__tenant").aget(
                id=run_id
            )
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        in_progress = {
            MaterializationRun.RunState.STARTED,
            MaterializationRun.RunState.DISCOVERING,
            MaterializationRun.RunState.LOADING,
            MaterializationRun.RunState.TRANSFORMING,
        }
        if run.state not in in_progress:
            tc["result"] = error_response(
                VALIDATION_ERROR,
                f"Run '{run_id}' is not in progress (state: {run.state})",
            )
            return tc["result"]

        previous_state = run.state
        run.state = MaterializationRun.RunState.FAILED
        run.completed_at = datetime.now(UTC)
        run.result = {**(run.result or {}), "cancelled": True}
        await run.asave(update_fields=["state", "completed_at", "result"])

        tenant_id = run.tenant_schema.tenant.external_id
        schema = run.tenant_schema.schema_name
        logger.info("Cancelled run %s for tenant %s (was: %s)", run_id, tenant_id, previous_state)

        tc["result"] = success_response(
            {"run_id": run_id, "cancelled": True, "previous_state": previous_state},
            schema=schema,
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


async def _resolve_workspace_memberships(workspace_id, user_id):
    """Resolve TenantMemberships for all tenants in a workspace."""
    workspace = await Workspace.objects.filter(id=workspace_id).afirst()
    if workspace is None:
        return None, f"Workspace '{workspace_id}' not found"

    tenant_ids = [
        wt.tenant_id
        async for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant")
    ]
    if not tenant_ids:
        return None, "Workspace has no tenants configured"

    qs = TenantMembership.objects.select_related("user", "tenant").filter(tenant_id__in=tenant_ids)
    if user_id:
        qs = qs.filter(user_id=user_id)

    memberships = [tm async for tm in qs]
    if not memberships:
        return None, "No tenant memberships found for this user in this workspace"

    return memberships, None


_MATERIALIZATION_POLL_INTERVAL_S = 3
_MATERIALIZATION_POLL_DEADLINE_S = 600


def _format_progress_message(progress: dict, multi_tenant: bool) -> str:
    """Render a user-facing progress string from a MaterializationRun.progress dict.

    Examples (single-tenant):
        Provisioning schema for my-experiment...
        Loading sessions from OCS API... 4.6% (500 / 13,028 rows)
        Loading sessions from OCS API... 500 rows loaded

    For multi-tenant workspaces the message is prefixed with ``[tenant-id]``.
    """
    base = progress.get("message") or "Working..."
    rows_loaded = progress.get("rows_loaded") or 0
    rows_total = progress.get("rows_total")
    source = progress.get("source")

    if rows_loaded:
        if rows_total:
            pct = (rows_loaded / rows_total) * 100 if rows_total else 0
            base = f"{base} {pct:.1f}% ({rows_loaded:,} / {rows_total:,} rows)"
        else:
            base = f"{base} {rows_loaded:,} rows loaded"
    elif source and not progress.get("message"):
        base = f"Working on {source}..."

    if multi_tenant:
        tenant_id = progress.get("tenant_id")
        if tenant_id:
            base = f"[{tenant_id}] {base}"
    return base


_PROCRASTINATE_TERMINAL_STATUSES = frozenset(
    {
        _ProcrastinateStatus.SUCCEEDED,
        _ProcrastinateStatus.FAILED,
        _ProcrastinateStatus.CANCELLED,
        _ProcrastinateStatus.ABORTED,
    }
)


async def _is_procrastinate_job_finished(job_id: int) -> bool:
    """True if the procrastinate job has reached a terminal state.

    Used by the workspace-progress poller as a backstop: the worker can
    legitimately skip a membership (no pipeline / no credential) without
    creating a MaterializationRun row, which would otherwise leave the
    poller waiting for runs that never appear.
    """
    try:
        status = await _procrastinate_app.job_manager.get_job_status_async(job_id)
    except Exception:
        logger.warning("Failed to fetch procrastinate job %s status", job_id, exc_info=True)
        return False
    return status in _PROCRASTINATE_TERMINAL_STATUSES


async def _query_workspace_progress(workspace_id: str, job_id: int, expected_count: int) -> dict:
    """Aggregate progress across all MaterializationRuns for this dispatched job.

    Returns:
        ``{
            "all_done": bool,            # every expected run reached a terminal state
            "any_cancelled": bool,
            "active_progress": dict | None,   # the latest active run's progress dict
            "tenants": list[dict],            # per-tenant completed/failed/cancelled summaries
            "queued": bool,                   # True if the worker hasn't created any rows yet
        }``
    """
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=job_id
        ).select_related("tenant_schema__tenant")
    ]
    job_finished = await _is_procrastinate_job_finished(job_id)
    if not runs:
        # No rows yet — but if the procrastinate job has finished, it means
        # every membership was skipped (e.g. all lacked pipelines or
        # credentials). Treat that as done so the poll loop exits.
        return {
            "all_done": job_finished,
            "any_cancelled": False,
            "active_progress": None,
            "tenants": [],
            "queued": not job_finished,
            "multi_tenant": expected_count > 1,
        }

    terminal_states = {
        MaterializationRun.RunState.COMPLETED,
        MaterializationRun.RunState.FAILED,
        MaterializationRun.RunState.CANCELLED,
    }
    active_progress: dict | None = None
    any_cancelled = False
    completed_runs = 0
    tenants: list[dict] = []
    multi_tenant = expected_count > 1

    # Sort by started_at ascending so the most-recent active run wins active_progress.
    runs.sort(key=lambda r: r.started_at)
    for run in runs:
        tenant_id = run.tenant_schema.tenant.external_id
        if run.state in terminal_states:
            completed_runs += 1
            tenants.append(
                {
                    "tenant": tenant_id,
                    "state": run.state,
                    "result": run.result,
                }
            )
            if run.state == MaterializationRun.RunState.CANCELLED:
                any_cancelled = True
        else:
            # The latest in-progress run drives the live message.
            progress = dict(run.progress or {})
            progress.setdefault("tenant_id", tenant_id)
            active_progress = progress

    # Fall back to the procrastinate job state when fewer rows exist than
    # expected: the worker skips memberships without creating a run row, so
    # ``completed_runs >= expected_count`` would otherwise never hold and
    # the poll loop would time out.
    all_done = completed_runs >= expected_count or job_finished

    return {
        "all_done": all_done,
        "any_cancelled": any_cancelled,
        "active_progress": active_progress,
        "tenants": tenants,
        "queued": False,
        "multi_tenant": multi_tenant,
    }


@mcp.tool()
async def run_materialization(
    workspace_id: str = "",
    user_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Materialize data for all tenants in the workspace.

    Defers the work to the procrastinate ``materialize_workspace`` task, then
    polls ``MaterializationRun.progress`` every few seconds and emits MCP
    progress notifications so the chat UI can show live updates without
    holding open a long-running HTTP request to the loaders.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: User UUID (injected server-side by the agent graph).
    """
    async with tool_context("run_materialization", workspace_id) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        memberships, err = await _resolve_workspace_memberships(workspace_id, user_id)
        if err:
            tc["result"] = error_response(NOT_FOUND, err)
            return tc["result"]

        expected_count = len(memberships)
        try:
            job = await materialize_workspace.defer_async(
                workspace_id=str(workspace_id),
                user_id=str(user_id) if user_id else "",
            )
        except Exception:
            logger.exception("Failed to dispatch materialize_workspace task")
            tc["result"] = error_response(INTERNAL_ERROR, "Failed to dispatch materialization task")
            return tc["result"]
        # ``defer_async`` returns a JobDeferResult-like object exposing the
        # job id; some procrastinate versions return the integer directly.
        job_id = getattr(job, "id", job) if not isinstance(job, int) else job

        deadline = time.monotonic() + _MATERIALIZATION_POLL_DEADLINE_S
        last_message: str | None = None
        progress_summary: dict | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(_MATERIALIZATION_POLL_INTERVAL_S)
            progress_summary = await _query_workspace_progress(workspace_id, job_id, expected_count)

            if ctx is not None and progress_summary["active_progress"]:
                p = progress_summary["active_progress"]
                step = p.get("step") or 0
                total_steps = p.get("total_steps") or 1
                message = _format_progress_message(p, progress_summary["multi_tenant"])
                if message != last_message:
                    try:
                        await ctx.report_progress(step, total_steps, message)
                    except Exception:
                        logger.warning("Progress notification delivery failed", exc_info=True)
                    last_message = message

            if progress_summary["all_done"] or progress_summary["any_cancelled"]:
                break
        else:
            # Polling deadline reached without reaching a terminal state.
            tc["result"] = success_response(
                {
                    "status": "timeout",
                    "tenants": progress_summary["tenants"] if progress_summary else [],
                    "all_succeeded": False,
                },
                schema="",
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        if progress_summary is None:
            progress_summary = await _query_workspace_progress(workspace_id, job_id, expected_count)

        results = []
        for t in progress_summary["tenants"]:
            state = t["state"]
            if state == MaterializationRun.RunState.COMPLETED:
                results.append({"tenant": t["tenant"], "success": True, "result": t["result"]})
            elif state == MaterializationRun.RunState.CANCELLED:
                results.append({"tenant": t["tenant"], "success": False, "cancelled": True})
            else:  # FAILED
                err_msg = "Pipeline failed"
                if isinstance(t.get("result"), dict):
                    err_msg = t["result"].get("error") or err_msg
                results.append({"tenant": t["tenant"], "success": False, "error": err_msg})

        all_succeeded = bool(results) and all(r.get("success") for r in results)
        status = (
            "cancelled"
            if progress_summary["any_cancelled"]
            else ("completed" if all_succeeded else "failed")
        )
        tc["result"] = success_response(
            {
                "status": status,
                "tenants": results,
                "all_succeeded": all_succeeded,
            },
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_schema_status(workspace_id: str = "") -> dict:
    """Check whether data has been loaded for this workspace.

    Returns schema existence, state, last materialization timestamp, and table
    list. Always succeeds — returns exists=False if no schema has been
    provisioned yet. Safe to call before any data has been loaded.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("get_schema_status", workspace_id) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        not_provisioned = success_response(
            {
                "exists": False,
                "state": "not_provisioned",
                "last_materialized_at": None,
                "tables": [],
            },
            schema="",
        )

        try:
            workspace = await Workspace.objects.aget(id=workspace_id)
        except Workspace.DoesNotExist:
            tc["result"] = not_provisioned
            return tc["result"]

        tenant_count = await workspace.tenants.acount()

        if tenant_count == 0:
            tc["result"] = not_provisioned
            return tc["result"]

        if tenant_count == 1:
            # Single-tenant: check TenantSchema directly
            tenant = await workspace.tenants.afirst()
            ts = await TenantSchema.objects.filter(
                tenant=tenant,
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            ).afirst()

            if ts is None:
                tc["result"] = not_provisioned
                return tc["result"]

            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=ts,
                    state=MaterializationRun.RunState.COMPLETED,
                )
                .order_by("-completed_at")
                .afirst()
            )

            last_materialized_at = None
            tables = []
            if last_run:
                if last_run.completed_at:
                    last_materialized_at = last_run.completed_at.isoformat()
                result_data = last_run.result or {}
                if "tables" in result_data:
                    tables = result_data["tables"]
                elif "table" in result_data and "rows_loaded" in result_data:
                    tables = [
                        {"name": result_data["table"], "row_count": result_data["rows_loaded"]}
                    ]

            tc["result"] = success_response(
                {
                    "exists": True,
                    "state": ts.state,
                    "last_materialized_at": last_materialized_at,
                    "tables": tables,
                },
                schema=ts.schema_name,
            )
            return tc["result"]

        # Multi-tenant: check WorkspaceViewSchema + per-tenant materialization
        vs = await WorkspaceViewSchema.objects.filter(
            workspace_id=workspace_id,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()

        if vs is None:
            tc["result"] = not_provisioned
            return tc["result"]

        # Collect last materialization time across all tenant schemas
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema__tenant_id__in=tenant_ids,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .afirst()
        )
        last_materialized_at = None
        if last_run and last_run.completed_at:
            last_materialized_at = last_run.completed_at.isoformat()

        # List tables from the view schema via information_schema
        ctx = await _resolve_mcp_context(workspace_id)
        tables = await workspace_list_tables(ctx)

        tc["result"] = success_response(
            {
                "exists": True,
                "state": vs.state,
                "last_materialized_at": last_materialized_at,
                "tables": tables,
            },
            schema=vs.schema_name,
        )
        return tc["result"]


@mcp.tool()
async def teardown_schema(confirm: bool = False, workspace_id: str = "") -> dict:
    """Drop all materialized data for this workspace.

    Destructive — all tenant schemas and the workspace view schema are
    permanently dropped. Schemas will be re-provisioned automatically on
    the next materialization run. Metadata extracted during materialization
    (CommCare app structure, field definitions) is stored separately and
    is NOT affected.

    Only call this when the user explicitly requests a data reset, or when
    a failed materialization has left the schema in an unrecoverable state.

    Args:
        confirm: Must be True to execute. Defaults to False as a safety guard.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
    """
    async with tool_context("teardown_schema", workspace_id, confirm=confirm) as tc:
        if not confirm:
            tc["result"] = error_response(
                VALIDATION_ERROR,
                "Pass confirm=True to tear down the schema. "
                "This will permanently drop all materialized data.",
            )
            return tc["result"]

        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]

        workspace = await Workspace.objects.filter(id=workspace_id).afirst()
        if workspace is None:
            tc["result"] = error_response(NOT_FOUND, f"Workspace '{workspace_id}' not found")
            return tc["result"]

        mgr = SchemaManager()
        dropped = []

        # Tear down the workspace view schema if it exists
        vs = (
            await WorkspaceViewSchema.objects.filter(
                workspace=workspace,
            )
            .exclude(state=SchemaState.TEARDOWN)
            .afirst()
        )
        if vs:
            await mgr.ateardown_view_schema(vs)
            dropped.append(vs.schema_name)

        # Tear down all tenant schemas for this workspace
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        async for ts in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids,
        ).exclude(state=SchemaState.TEARDOWN):
            schema_name = ts.schema_name
            await mgr.ateardown(ts)
            dropped.append(schema_name)

        tc["result"] = success_response(
            {"schemas_dropped": dropped},
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


# --- Server setup ---


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # never write to stdout with stdio transport
    )


def _setup_django() -> None:
    """Initialize Django ORM for model access.

    Requires DJANGO_SETTINGS_MODULE to be set in the environment.
    Does NOT default to development settings to avoid accidentally
    running with DEBUG=True in production.
    """
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        raise RuntimeError(
            "DJANGO_SETTINGS_MODULE environment variable is required. "
            "Set it to 'config.settings.development' or 'config.settings.production'."
        )
    import django

    django.setup()


def _run_server(args: argparse.Namespace) -> None:
    """Start the MCP server (called directly or as a reload target)."""
    _configure_logging(args.verbose)
    _setup_django()

    logger.info("Starting Scout MCP server (transport=%s)", args.transport)

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # Allow internal Docker network hostname in addition to loopback defaults.
        # The MCP server is internal-only; DNS rebinding protection is still on.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "scout-mcp-web:*"],
        )

    mcp.run(transport=args.transport)


def _run_with_reload(args: argparse.Namespace) -> None:
    """Run the server in a subprocess and restart it when files change."""
    import subprocess

    from watchfiles import watch

    watch_dirs = ["mcp_server", "apps"]
    cmd = [
        sys.executable,
        "-m",
        "mcp_server",
        "--transport",
        args.transport,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.verbose:
        cmd.append("--verbose")

    _configure_logging(args.verbose)
    logger.info("Watching %s for changes (reload enabled)", ", ".join(watch_dirs))

    process = subprocess.Popen(cmd)  # noqa: S603 — cmd list built from argparse, no shell
    try:
        for changes in watch(*watch_dirs, watch_filter=lambda _, path: path.endswith(".py")):
            changed = [str(c[1]) for c in changes]
            logger.info("Detected changes in %s — restarting", ", ".join(changed))
            process.terminate()
            process.wait()
            process = subprocess.Popen(cmd)  # noqa: S603 — cmd list built from argparse, no shell
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()
        process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scout MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8100, help="HTTP port (default: 8100)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (development only)",
    )

    args = parser.parse_args()

    if args.reload:
        _run_with_reload(args)
    else:
        _run_server(args)


if __name__ == "__main__":
    main()
