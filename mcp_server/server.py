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
import contextlib
import logging
import os
import sys
import uuid
from datetime import UTC, datetime

from django.core.exceptions import ValidationError as _ValidationError
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from procrastinate.contrib.django.procrastinate_app import current_app as _procrastinate_app

from apps.chat.models import Thread, ThreadJob
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
    SCHEMA_BUILD_FAILED,
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
async def list_tables(workspace_id: str = "", user_id: str = "", thread_id: str = "") -> dict:
    """List all tables in the workspace's database schema.

    Returns table names, types, descriptions, row counts, and materialization timestamps.
    Returns an empty list if no materialization run has completed yet.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: Acting user UUID (injected server-side; recorded in the audit trail).
        thread_id: Chat thread UUID (injected server-side; recorded in the audit trail).
    """
    async with tool_context(
        "list_tables", workspace_id, user_id=user_id, thread_id=thread_id
    ) as tc:
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
                state__in=[
                    MaterializationRun.RunState.COMPLETED,
                    MaterializationRun.RunState.PARTIAL,
                ],
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
async def describe_table(
    table_name: str, workspace_id: str = "", user_id: str = "", thread_id: str = ""
) -> dict:
    """Get detailed metadata for a specific table.

    Returns columns (name, type, nullable, default, description) and a table description.
    JSONB columns are annotated with summaries from the CommCare discover phase when available.

    Args:
        table_name: Name of the table to describe.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: Acting user UUID (injected server-side; recorded in the audit trail).
        thread_id: Chat thread UUID (injected server-side; recorded in the audit trail).
    """
    async with tool_context(
        "describe_table", workspace_id, user_id=user_id, thread_id=thread_id, table_name=table_name
    ) as tc:
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
                    state__in=[
                        MaterializationRun.RunState.COMPLETED,
                        MaterializationRun.RunState.PARTIAL,
                    ],
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
async def get_metadata(workspace_id: str = "", user_id: str = "", thread_id: str = "") -> dict:
    """Get a complete metadata snapshot for the workspace's database.

    Returns all tables with their columns, descriptions, and table relationships
    defined by the materialization pipeline.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: Acting user UUID (injected server-side; recorded in the audit trail).
        thread_id: Chat thread UUID (injected server-side; recorded in the audit trail).
    """
    async with tool_context(
        "get_metadata", workspace_id, user_id=user_id, thread_id=thread_id
    ) as tc:
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
                state__in=[
                    MaterializationRun.RunState.COMPLETED,
                    MaterializationRun.RunState.PARTIAL,
                ],
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
async def get_lineage(
    model_name: str, workspace_id: str = "", user_id: str = "", thread_id: str = ""
) -> dict:
    """Get the transformation lineage for a model.

    Returns the chain of transformations from the given model back to the raw
    source data, showing what each step does and why. Use this when the user
    asks about data provenance, how a table was created, or what cleaning
    or transformations were applied to the data.

    Args:
        model_name: Name of the model to trace lineage for.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: Acting user UUID (injected server-side; recorded in the audit trail).
        thread_id: Chat thread UUID (injected server-side; recorded in the audit trail).
    """
    async with tool_context(
        "get_lineage", workspace_id, user_id=user_id, thread_id=thread_id, model_name=model_name
    ) as tc:
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
async def query(sql: str, workspace_id: str = "", user_id: str = "", thread_id: str = "") -> dict:
    """Execute a read-only SQL query against the workspace's database.

    The query is validated for safety (SELECT only, no dangerous functions),
    row limits are enforced, and execution uses a read-only database role.

    Args:
        sql: A SQL SELECT query to execute.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: Acting user UUID (injected server-side; recorded in the audit trail).
        thread_id: Chat thread UUID (injected server-side; recorded in the audit trail).
    """
    async with tool_context(
        "query", workspace_id, user_id=user_id, thread_id=thread_id, sql=sql
    ) as tc:
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


@mcp.tool()
async def run_materialization(
    workspace_id: str = "",
    user_id: str = "",
    thread_id: str = "",
    tool_call_id: str = "",
    ctx: Context | None = None,
) -> dict:
    """Start a materialization in the background and acknowledge immediately.

    Defers the work to the procrastinate ``materialize_workspace`` task and
    creates a ThreadJob row tying that procrastinate job to the calling chat
    thread. Returns ``status: started`` right away — the chat agent should
    acknowledge briefly to the user and end its turn. When materialization
    finishes, a chained ``resume_thread_after_materialization`` task injects
    completion into the conversation via the LangGraph checkpointer.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: User UUID (injected server-side).
        thread_id: Chat thread UUID (injected server-side).
        tool_call_id: LangChain tool_call_id for this invocation (injected
            server-side); persisted on ThreadJob so the resume task can
            attribute its work to the right call.
    """
    async with tool_context(
        "run_materialization", workspace_id, user_id=user_id, thread_id=thread_id
    ) as tc:
        if not workspace_id:
            tc["result"] = error_response(VALIDATION_ERROR, "workspace_id is required")
            return tc["result"]
        if not thread_id:
            tc["result"] = error_response(VALIDATION_ERROR, "thread_id is required")
            return tc["result"]

        # thread_id is injected server-side and must be a real chat Thread UUID.
        # A non-UUID value (e.g. the recipe runner's synthetic "recipe-run-<id>")
        # must fail cleanly here rather than crash the UUIDField cast in the
        # Thread lookup below. This tool is interactive/fire-and-resume only;
        # headless callers (recipes) use the blocking agent-side materialize
        # tool, which never reaches this code path.
        try:
            uuid.UUID(str(thread_id))
        except (ValueError, AttributeError, TypeError):
            tc["result"] = error_response(
                VALIDATION_ERROR, "thread_id must be a valid thread identifier"
            )
            return tc["result"]

        # Authorization guard: confirms the user has at least one tenant
        # membership in this workspace before we dispatch a job.
        _, err = await _resolve_workspace_memberships(workspace_id, user_id)
        if err:
            tc["result"] = error_response(NOT_FOUND, err)
            return tc["result"]

        # Defense in depth: even though the chat layer validates thread
        # ownership, the MCP tool also checks before binding a ThreadJob to
        # the thread, since this tool is the one persisting the trust boundary.
        thread_exists = await Thread.objects.filter(
            id=thread_id,
            user_id=user_id,
            workspace_id=workspace_id,
        ).aexists()
        if not thread_exists:
            tc["result"] = error_response(NOT_FOUND, "thread not found in this workspace")
            return tc["result"]

        # Guard against concurrent dispatch in the SAME thread: if this chat
        # already has a materialization in flight, return its identity so the
        # agent can tell the user to wait. We scope the guard by thread_id
        # (not workspace) because the chained resume task only fires once
        # against the original ThreadJob — a second caller in a different
        # thread would otherwise get no follow-up message when the worker
        # finishes (resume defers to a single thread_job_id). Note: this lets
        # two threads in the same workspace dispatch parallel materializations
        # that share tenant_schemas. This is not new: the prior workspace-
        # scoped guard already permitted parallel runs across *different*
        # workspaces sharing a tenant (multi-workspace tenants), and the
        # materializer has no advisory lock per tenant_schema. If we ever add
        # tenant-level dedupe we should add it here with a tenant_id filter
        # rather than the workspace_id we removed.
        existing = await ThreadJob.objects.filter(
            thread_id=thread_id,
            job_type=ThreadJob.JobType.MATERIALIZATION,
            state__in=list(ThreadJob.ACTIVE_STATES),
        ).afirst()
        if existing is not None:
            tc["result"] = success_response(
                {
                    "status": "already_in_progress",
                    "thread_job_id": str(existing.id),
                    "message": (
                        "A materialization is already running in this chat. "
                        "I'll continue once it finishes."
                    ),
                },
                schema="",
                timing_ms=tc["timer"].elapsed_ms,
            )
            return tc["result"]

        try:
            job = await materialize_workspace.defer_async(
                workspace_id=str(workspace_id),
                user_id=str(user_id) if user_id else "",
            )
        except Exception:
            logger.exception("Failed to dispatch materialize_workspace task")
            tc["result"] = error_response(INTERNAL_ERROR, "Failed to dispatch materialization task")
            return tc["result"]
        job_id = getattr(job, "id", job) if not isinstance(job, int) else job

        # Atomicity note: defer_async and ThreadJob.acreate are not in a single
        # transaction. If acreate fails after the worker has already picked up
        # the job, abort=True is best-effort (procrastinate only honors it at
        # cooperative await points). The janitor task (expire_stale_thread_jobs)
        # cleans up any orphaned runs.
        try:
            tj = await ThreadJob.objects.acreate(
                thread_id=thread_id,
                job_type=ThreadJob.JobType.MATERIALIZATION,
                procrastinate_job_id=job_id,
                tool_call_id=tool_call_id,
                state=ThreadJob.State.PENDING,
            )
        except Exception:
            logger.exception("Failed to create ThreadJob; rolling back dispatch")
            with contextlib.suppress(Exception):
                await _procrastinate_app.job_manager.cancel_job_by_id_async(job_id, abort=True)
            tc["result"] = error_response(INTERNAL_ERROR, "Failed to track job")
            return tc["result"]

        tc["result"] = success_response(
            {
                "status": "started",
                "thread_job_id": str(tj.id),
                "message": (
                    "Materialization started in background. I'll continue when it finishes."
                ),
            },
            schema="",
            timing_ms=tc["timer"].elapsed_ms,
        )
        return tc["result"]


@mcp.tool()
async def get_schema_status(workspace_id: str = "", user_id: str = "", thread_id: str = "") -> dict:
    """Check whether data has been loaded for this workspace.

    Returns schema existence, state, last materialization timestamp, and table
    list. Always succeeds — returns exists=False if no schema has been
    provisioned yet. Safe to call before any data has been loaded.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).
        user_id: Acting user UUID (injected server-side; recorded in the audit trail).
        thread_id: Chat thread UUID (injected server-side; recorded in the audit trail).
    """
    async with tool_context(
        "get_schema_status", workspace_id, user_id=user_id, thread_id=thread_id
    ) as tc:
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
                    state__in=[
                        MaterializationRun.RunState.COMPLETED,
                        MaterializationRun.RunState.PARTIAL,
                    ],
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
            # No usable view schema. Distinguish a genuine never-built state
            # from a FAILED build: the latter means the per-tenant data loaded
            # but the workspace query layer could not be assembled, which the
            # agent must surface (and must NOT mistake for "just run
            # materialization"). Additive fields only — shape stays compatible.
            failed_vs = await WorkspaceViewSchema.objects.filter(
                workspace_id=workspace_id,
                state=SchemaState.FAILED,
            ).afirst()
            if failed_vs is not None:
                # Return a TOP-LEVEL error envelope (success=False), not a
                # success envelope carrying ``data.error`` (arch #246, 13#6).
                # A success-on-failure is mis-classified as success by both the
                # rich cards and the agent (the "agent told completed" class):
                # the agent must surface this build failure, not silently treat
                # the workspace as queryable or invite a pointless re-run.
                tc["result"] = error_response(
                    SCHEMA_BUILD_FAILED,
                    "The workspace view schema failed to build.",
                    detail=failed_vs.last_error or "View schema build failed.",
                )
                return tc["result"]
            tc["result"] = not_provisioned
            return tc["result"]

        # Collect last materialization time across all tenant schemas
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        last_run = (
            await MaterializationRun.objects.filter(
                tenant_schema__tenant_id__in=tenant_ids,
                state__in=[
                    MaterializationRun.RunState.COMPLETED,
                    MaterializationRun.RunState.PARTIAL,
                ],
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

    NOT exposed to the agent (arch #237 / finding 00#2): this tool DROPs
    physical schemas but updates no Django state and does not fail dependent
    sibling workspaces, so it is filtered out of the agent's tool set
    (``AGENT_EXCLUDED_MCP_TOOLS`` in ``apps/agents/graph/base.py``). It remains
    defined for operator/HTTP callers only. Legitimate teardown for the agent's
    workflow happens via the worker ``teardown_schema`` task (TTL expiry /
    refresh), which performs the full state update + sibling-fail machinery.

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
