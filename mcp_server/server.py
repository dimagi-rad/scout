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

import uvicorn
from django.conf import settings
from django.core.exceptions import ValidationError as _ValidationError
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from apps.chat.models import Thread, ThreadJob
from apps.transformations.services.lineage import aget_lineage_chain
from apps.users.models import TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.catalog import (
    CatalogContext,
    catalog_metadata,
    describe,
    list_catalog,
    to_tool_dict,
)
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    aresolve_pipeline_config,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.services.tenant_metadata import aget_tenant_metadata
from apps.workspaces.services.world_state import derive_world_state
from apps.workspaces.tasks import materialize_workspace
from config.procrastinate import app as procrastinate_app
from mcp_server.auth import SharedSecretMiddleware
from mcp_server.context import load_workspace_context
from mcp_server.envelope import (
    INTERNAL_ERROR,
    NOT_FOUND,
    PIPELINE_UNRESOLVED,
    SCHEMA_BUILD_FAILED,
    VALIDATION_ERROR,
    error_response,
    success_response,
    tool_context,
)
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.query import execute_query

logger = logging.getLogger(__name__)

mcp = FastMCP("scout")


async def _resolve_mcp_context(workspace_id: str):
    """Load a QueryContext for the workspace."""
    if not workspace_id:
        raise ValueError("workspace_id is required")
    return await load_workspace_context(workspace_id)


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

        # Multi-tenant workspaces point at a WorkspaceViewSchema (namespaced
        # views): the catalog reconciles them from information_schema directly.
        if workspace_id:
            is_view_schema = await WorkspaceViewSchema.objects.filter(
                schema_name=ctx.schema_name, state=SchemaState.ACTIVE
            ).aexists()
            if is_view_schema:
                catalog_ctx = CatalogContext(
                    query_context=ctx,
                    is_view_schema=True,
                    tenant_schema=None,
                    pipeline_config=None,
                    workspace_id=workspace_id,
                )
                tables = [to_tool_dict(t) for t in await list_catalog(catalog_ctx)]
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
        try:
            pipeline_config = await aresolve_pipeline_config(ts, last_run)
        except PipelineResolutionError as e:
            tc["result"] = error_response(PIPELINE_UNRESOLVED, str(e))
            return tc["result"]

        catalog_ctx = CatalogContext(
            query_context=ctx,
            is_view_schema=False,
            tenant_schema=ts,
            pipeline_config=pipeline_config,
            tenant_ids=[ts.tenant_id],
            workspace_id=workspace_id or None,
        )
        tables = [to_tool_dict(t) for t in await list_catalog(catalog_ctx)]

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
        pipeline_config = None
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
            tenant_metadata = await aget_tenant_metadata(ts.tenant_id)
            try:
                pipeline_config = await aresolve_pipeline_config(ts, last_run)
            except PipelineResolutionError as e:
                tc["result"] = error_response(PIPELINE_UNRESOLVED, str(e))
                return tc["result"]

        # ts is None only for a multi-tenant ws_* view schema, where no single
        # tenant pipeline exists to infer: pass None and let describe read columns
        # from information_schema directly.
        catalog_ctx = CatalogContext(
            query_context=ctx,
            is_view_schema=ts is None,
            tenant_schema=ts,
            pipeline_config=pipeline_config,
            tenant_ids=[ts.tenant_id] if ts is not None else [],
            workspace_id=workspace_id or None,
        )
        table = await describe(catalog_ctx, table_name, tenant_metadata)
        if table is None:
            tc["result"] = error_response(
                NOT_FOUND, f"Table '{table_name}' not found in schema '{ctx.schema_name}'"
            )
            return tc["result"]

        tc["result"] = success_response(
            table.as_dict(),
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

        last_run = None
        tenant_metadata = None
        pipeline_config = None
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
            tenant_metadata = await aget_tenant_metadata(ts.tenant_id)
            try:
                pipeline_config = await aresolve_pipeline_config(ts, last_run)
            except PipelineResolutionError as e:
                tc["result"] = error_response(PIPELINE_UNRESOLVED, str(e))
                return tc["result"]

        # ts is None here only for a multi-tenant ws_* view schema (an
        # unprovisioned workspace raises in _resolve_mcp_context above). The
        # catalog reads columns from the ws_* information_schema directly, so
        # multi-tenant get_metadata now returns real tables/columns instead of
        # the old table_count=0 (the ws_* lookup-miss fix).
        catalog_ctx = CatalogContext(
            query_context=ctx,
            is_view_schema=ts is None,
            tenant_schema=ts,
            pipeline_config=pipeline_config,
            tenant_ids=[ts.tenant_id] if ts is not None else [],
            workspace_id=workspace_id or None,
        )
        metadata = await catalog_metadata(catalog_ctx, tenant_metadata)

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
async def get_materialization_status(run_id: str, workspace_id: str = "") -> dict:
    """Retrieve the status of a materialization run by ID.

    Primarily a fallback for reconnection scenarios — live progress is delivered
    via MCP progress notifications during an active run_materialization call.

    Args:
        run_id: UUID of the MaterializationRun to look up.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
            The run is scoped to this workspace (arch #253, 01#6) so a run in
            another workspace cannot be inspected from here.
    """
    async with tool_context("get_materialization_status", run_id, workspace_id=workspace_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related("tenant_schema__tenant").aget(
                id=run_id
            )
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        if not await _run_belongs_to_workspace(run, workspace_id):
            # Same NOT_FOUND as a genuinely missing run so a caller can't probe
            # the existence of runs in other workspaces.
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
async def cancel_materialization(run_id: str, workspace_id: str = "") -> dict:
    """Cancel a running materialization pipeline.

    Marks the run as CANCELLED in the database. This is a best-effort
    cancellation — in-flight loader operations may not terminate immediately.
    Full subprocess cancellation is a future feature.

    Args:
        run_id: UUID of the MaterializationRun to cancel.
        workspace_id: Workspace UUID (injected server-side by the agent graph).
            The run is scoped to this workspace (arch #253, 01#6) so a run in
            another workspace cannot be cancelled from here.
    """
    async with tool_context("cancel_materialization", run_id, workspace_id=workspace_id) as tc:
        try:
            run = await MaterializationRun.objects.select_related("tenant_schema__tenant").aget(
                id=run_id
            )
        except (MaterializationRun.DoesNotExist, ValueError, _ValidationError):
            tc["result"] = error_response(NOT_FOUND, f"Materialization run '{run_id}' not found")
            return tc["result"]

        if not await _run_belongs_to_workspace(run, workspace_id):
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
        # Dedicated CANCELLED state (not FAILED) keeps a deliberate cancel
        # distinguishable from a real failure, matching the user-facing path
        # (apps/workspaces/api/jobs_cancel.py::cancel_thread_job). The
        # result.cancelled flag is retained for back-compat (#290).
        run.state = MaterializationRun.RunState.CANCELLED
        run.completed_at = datetime.now(UTC)
        run.result = {**(run.result or {}), "cancelled": True}
        await run.asave(update_fields=["state", "completed_at", "result"])

        # The DB flip above is the stop signal, but alone it left the procrastinate
        # job running and the ThreadJob spinning. Mirror the HTTP cancel path: abort
        # the job and flip its ThreadJob so a mid-load cancel unwinds (arch #255 01#1).
        if run.procrastinate_job_id is not None:
            with contextlib.suppress(Exception):
                await procrastinate_app.job_manager.cancel_job_by_id_async(
                    run.procrastinate_job_id, abort=True
                )
            await ThreadJob.objects.filter(
                procrastinate_job_id=run.procrastinate_job_id,
                state__in=list(ThreadJob.ACTIVE_STATES),
            ).aupdate(state=ThreadJob.State.CANCELLED, completed_at=datetime.now(UTC))

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
    """Resolve a user's TenantMemberships within a workspace.

    Entitlement guard (arch #253, 01#6): ``user_id`` is REQUIRED. An empty
    ``user_id`` previously skipped the user filter and returned every membership
    in the workspace, so the membership guard passed for any/no user. We now
    reject an empty ``user_id`` outright — a server-injected acting user is the
    whole point of the check.
    """
    if not user_id:
        return None, "user_id is required"

    workspace = await Workspace.objects.filter(id=workspace_id).afirst()
    if workspace is None:
        return None, f"Workspace '{workspace_id}' not found"

    tenant_ids = [
        wt.tenant_id
        async for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant")
    ]
    if not tenant_ids:
        return None, "Workspace has no tenants configured"

    memberships = [
        tm
        async for tm in TenantMembership.objects.select_related("user", "tenant").filter(
            tenant_id__in=tenant_ids, user_id=user_id
        )
    ]
    if not memberships:
        return None, "No tenant memberships found for this user in this workspace"

    return memberships, None


async def _run_belongs_to_workspace(run, workspace_id) -> bool:
    """Return True if a MaterializationRun's tenant is part of ``workspace_id``.

    Scopes LLM-supplied ``run_id``s to the calling workspace (arch #253, 01#6)
    so a run in another workspace cannot be inspected or cancelled from a chat
    scoped elsewhere.
    """
    if not workspace_id:
        return False
    return await WorkspaceTenant.objects.filter(
        workspace_id=workspace_id,
        tenant_id=run.tenant_schema.tenant_id,
    ).aexists()


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

        # Confirm the user has a tenant membership here before dispatching.
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

        # Dedupe concurrent dispatch by thread_id (not workspace): the chained
        # resume task fires once against a single thread_job_id, so a second
        # caller in another thread would get no follow-up when the worker
        # finishes. Consequence: two threads in one workspace can run parallel
        # materializations sharing tenant_schemas — unchanged from the prior
        # workspace-scoped guard, and the materializer has no per-tenant_schema
        # lock. Tenant-level dedupe, if ever added, belongs here with a
        # tenant_id filter.
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
                await procrastinate_app.job_manager.cancel_job_by_id_async(job_id, abort=True)
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
            # A genuinely missing workspace is NOT the same as an existing-but-
            # unprovisioned one. Returning the empty 'not_provisioned' success
            # envelope here invited the agent to materialize against a phantom
            # workspace (07#7); surface a NOT_FOUND error so it stops instead.
            tc["result"] = error_response(NOT_FOUND, f"Workspace '{workspace_id}' not found")
            return tc["result"]

        tenant_count = await workspace.tenants.acount()

        if tenant_count == 0:
            tc["result"] = not_provisioned
            return tc["result"]

        # Canonical world-state read-model (arch #251): drives status,
        # last_materialized_at, and the in-progress predicate so this tool tells
        # the same story as the status API and the prompt builders.
        world = await derive_world_state(workspace)
        last_materialized_at = world.last_synced_at.isoformat() if world.last_synced_at else None

        if tenant_count == 1:
            tenant = await workspace.tenants.afirst()
            ts = await TenantSchema.objects.filter(
                tenant=tenant,
                state=SchemaState.ACTIVE,
            ).afirst()

            # Usable iff an ACTIVE schema exists OR a (re)build is running — the
            # same in-progress predicate the status API and prompt use, so this
            # tool can't report not_provisioned while a run is in flight.
            if ts is None and not world.in_progress:
                tc["result"] = not_provisioned
                return tc["result"]

            tables: list = []
            schema_name = ""
            if ts is not None:
                schema_name = ts.schema_name
                # The canonical reconciled catalog (arch #251, Phase 3): same
                # source-of-truth, fail-closed reconciliation, and stg_* policy the
                # status API, prompt, MCP list_tables, and DRF dictionary use, so
                # this tool tells the same story. An unresolvable pipeline degrades
                # to an empty table list rather than erroring — this status tool's
                # contract is "always succeeds" (the world_state story stays truthful).
                try:
                    pipeline_config = await aresolve_pipeline_config(ts, None)
                    catalog_ctx = CatalogContext(
                        query_context=None,
                        is_view_schema=False,
                        tenant_schema=ts,
                        pipeline_config=pipeline_config,
                        tenant_ids=[ts.tenant_id],
                        workspace_id=str(workspace_id),
                    )
                    tables = [to_tool_dict(t) for t in await list_catalog(catalog_ctx)]
                except PipelineResolutionError:
                    logger.warning(
                        "get_schema_status: no pipeline resolved for schema %s; "
                        "returning status without a table list",
                        ts.schema_name,
                    )

            tc["result"] = success_response(
                {
                    "exists": True,
                    "state": world.status,
                    "last_materialized_at": last_materialized_at,
                    "tables": tables,
                },
                schema=schema_name,
            )
            return tc["result"]

        # Multi-tenant: WorkspaceViewSchema + per-tenant materialization.
        if world.status == "failed":
            # A FAILED view schema means the per-tenant data loaded but the
            # workspace query layer could not be assembled. Return a TOP-LEVEL
            # error envelope (success=False), not a success-on-failure carrying
            # ``data.error`` (arch #246, 13#6): the latter is mis-classified as a
            # success by both the rich cards and the agent (the "agent told
            # completed" class), so the agent must surface this build failure, not
            # silently treat the workspace as queryable or invite a pointless re-run.
            failed_vs = await WorkspaceViewSchema.objects.filter(
                workspace_id=workspace_id,
                state=SchemaState.FAILED,
            ).afirst()
            tc["result"] = error_response(
                SCHEMA_BUILD_FAILED,
                "The workspace view schema failed to build.",
                detail=(
                    world.last_error
                    or (failed_vs.last_error if failed_vs else None)
                    or "View schema build failed."
                ),
            )
            return tc["result"]

        active_vs = await WorkspaceViewSchema.objects.filter(
            workspace_id=workspace_id,
            state=SchemaState.ACTIVE,
        ).afirst()

        # Usable iff an ACTIVE view schema exists OR a rebuild is in progress
        # (aligned with derive_world_state, so a rebuild window is never reported
        # as not_provisioned).
        if active_vs is None and not world.in_progress:
            tc["result"] = not_provisioned
            return tc["result"]

        tables = []
        schema_name = ""
        if active_vs is not None:
            schema_name = active_vs.schema_name
            ctx = await _resolve_mcp_context(workspace_id)
            catalog_ctx = CatalogContext(
                query_context=ctx,
                is_view_schema=True,
                tenant_schema=None,
                pipeline_config=None,
                workspace_id=str(workspace_id),
            )
            tables = [to_tool_dict(t) for t in await list_catalog(catalog_ctx)]

        tc["result"] = success_response(
            {
                "exists": True,
                "state": world.status,
                "last_materialized_at": last_materialized_at,
                "tables": tables,
            },
            schema=schema_name,
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
        _run_streamable_http(args)
        return

    mcp.run(transport=args.transport)


def _run_streamable_http(args: argparse.Namespace) -> None:
    """Serve the streamable-HTTP transport with shared-secret caller auth.

    We build the Starlette app ourselves (rather than calling ``mcp.run``) so we
    can wrap it in ``SharedSecretMiddleware`` (arch #253, 01#6) — the secret
    check then fires ahead of any MCP session/tool dispatch. DNS-rebinding Host
    protection stays on as a second layer.
    """
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    # Allow internal Docker network hostname in addition to loopback defaults.
    # The MCP server is internal-only; DNS rebinding protection is still on.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "scout-mcp-web:*"],
    )

    app = mcp.streamable_http_app()
    app.add_middleware(SharedSecretMiddleware, secret=settings.MCP_SHARED_SECRET)

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=("debug" if args.verbose else "info"),
    )
    uvicorn.Server(config).run()


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
