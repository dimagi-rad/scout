"""Shared physical, run, and semantic state for workspace query consumers."""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, OuterRef, Q, Subquery

from apps.semantic.models import CubeSchema, SemanticModel
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.tenant_coverage import parse_coverage


async def included_tenant_snapshot_state(workspace, tenant_coverage) -> str:
    coverage = parse_coverage(tenant_coverage)
    included_ids = (
        {entry["tenant_id"] for entry in coverage["included_tenants"]}
        if coverage is not None
        else {str(tenant.id) async for tenant in workspace.tenants.all()}
    )
    # PARTIAL data may be readable but cannot certify a complete new semantic
    # snapshot. Legacy ACTIVE schemas without run history remain usable; a known
    # failed/partial attempt must never inherit that legacy allowance.
    latest_start = (
        MaterializationRun.objects.filter(
            tenant_schema__tenant_id=OuterRef("tenant_schema__tenant_id")
        )
        .order_by("-started_at")
        .values("started_at")[:1]
    )
    # UUIDs cannot order tied timestamps, and an older live writer can still
    # alter the snapshot after a newer attempt completes.
    latest_runs = MaterializationRun.objects.filter(
        Q(started_at=Subquery(latest_start)) | Q(state__in=MaterializationRun.ACTIVE_STATES),
        tenant_schema__tenant__workspace_tenants__workspace=workspace,
    ).values_list("tenant_schema__tenant_id", "state")
    states = {state async for tenant_id, state in latest_runs if str(tenant_id) in included_ids}
    if states - MaterializationRun.ACTIVE_STATES - {MaterializationRun.RunState.COMPLETED}:
        return "unsafe"
    return "in_progress" if states & MaterializationRun.ACTIVE_STATES else "safe"


async def semantic_layer_state(workspace) -> tuple[str, str]:
    """Classify the workspace's semantic layer for the resume prompt.

    Returns ``(state, error)``:

    - ``"ready"`` — an active Cube schema exists and the latest build succeeded.
    - ``"deferred"`` — promotion is waiting for an included source still refreshing.
    - ``"stale"`` — an active schema is still serving, but the latest rebuild
      failed (recorded by ``build_and_promote_cube_schema`` in
      ``SemanticModel.metadata["last_build"]``); new tables/fields from this
      load may be missing.
    - ``"unavailable"`` — a model row exists but nothing is queryable.
    - ``"unknown"`` — no model row at all (a build was never attempted, e.g.
      legacy data); the agent's own tool errors are the honest signal there.
    """
    model = await SemanticModel.objects.filter(workspace=workspace).afirst()
    if model is None:
        return "unknown", ""
    last_build = (model.metadata or {}).get("last_build") or {}
    error = str(last_build.get("error") or "")
    has_active = await CubeSchema.objects.filter(
        workspace=workspace,
        semantic_model=model,
        status=CubeSchema.Status.ACTIVE,
    ).aexists()
    if not has_active or model.status != SemanticModel.Status.ACTIVE:
        return "unavailable", error or "no active semantic model and Cube schema are available"
    if last_build.get("status") == "deferred":
        if error:
            return "stale", error
        coverage = (
            await WorkspaceViewSchema.objects.filter(workspace=workspace, state=SchemaState.ACTIVE)
            .values_list("tenant_coverage", flat=True)
            .afirst()
        )
        snapshot_state = await included_tenant_snapshot_state(workspace, coverage)
        if snapshot_state == "in_progress":
            return "deferred", "An included source is still refreshing."
        if snapshot_state == "unsafe":
            return "stale", "An included source's refresh did not complete successfully."
        return "stale", "Source refreshes finished, but semantic promotion has not completed."
    if last_build and not last_build.get("ok", True):
        return "stale", error
    return "ready", ""


async def workspace_query_surface(workspace) -> dict[str, Any]:
    """Choose a repair without confusing missing views with missing source data.

    Snapshot safety and semantic staleness use the same rules as materialization
    and chat resume. An old ACTIVE model alone does not prove physical readiness.
    """
    tenant_ids = [
        tenant_id
        async for tenant_id in WorkspaceTenant.objects.filter(workspace=workspace).values_list(
            "tenant_id", flat=True
        )
    ]
    active_tenant_ids = {
        tenant_id
        async for tenant_id in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids, state=SchemaState.ACTIVE
        ).values_list("tenant_id", flat=True)
    }
    view = (
        await WorkspaceViewSchema.objects.filter(workspace=workspace).afirst()
        if len(tenant_ids) > 1
        else None
    )
    physical_ready = (
        view is not None and view.state == SchemaState.ACTIVE
        if len(tenant_ids) > 1
        else bool(active_tenant_ids)
    )
    physical_status = view.state if view else "active" if physical_ready else "missing"
    coverage = parse_coverage(view.tenant_coverage) if view and physical_ready else None
    runs = MaterializationRun.objects.filter(
        tenant_schema__tenant_id__in=tenant_ids,
        state__in=MaterializationRun.ACTIVE_STATES,
    )
    in_progress = await runs.aexists()
    serving_runs = runs
    if coverage is not None:
        excluded = {entry["tenant_id"] for entry in coverage["excluded_tenants"]} - {
            entry["tenant_id"] for entry in coverage["included_tenants"]
        }
        excluded_run_tenants = [
            tenant_id
            async for tenant_id in runs.values_list("tenant_schema__tenant_id", flat=True)
            if str(tenant_id) in excluded
        ]
        serving_runs = serving_runs.exclude(tenant_schema__tenant_id__in=excluded_run_tenants)
    unsafe_writer = (
        await serving_runs.annotate(
            has_serving_schema=Exists(
                TenantSchema.objects.filter(
                    tenant_id=OuterRef("tenant_schema__tenant_id"), state=SchemaState.ACTIVE
                )
            )
        )
        .exclude(tenant_schema__state=SchemaState.PROVISIONING, has_serving_schema=True)
        .aexists()
    )
    semantic_status, semantic_error = await semantic_layer_state(workspace)
    base = {
        "physical_status": physical_status,
        "semantic_status": semantic_status,
        "in_progress": in_progress,
        "queryable": False,
        "tenant_coverage": view.tenant_coverage if view and physical_ready else None,
    }
    if not tenant_ids:
        return {
            **base,
            "status": "unavailable",
            "recovery_action": None,
            "message": "This workspace has no data sources to restore.",
        }
    if unsafe_writer or (in_progress and not physical_ready):
        return {
            **base,
            "status": "recovering",
            "recovery_action": None,
            "message": "Workspace data is being refreshed. Waiting for a safe query surface.",
        }
    if not physical_ready:
        view_only = len(tenant_ids) > 1 and bool(active_tenant_ids)
        return {
            **base,
            "status": "needs_view_rebuild" if view_only else "needs_materialization",
            "recovery_action": "view_rebuild" if view_only else "materialization",
            "message": (
                "Source data is available. Rebuild the workspace query layer to use it again."
                if view_only
                else "The workspace data is no longer available. Restore it to use this artifact again."
            ),
            **({"detail": view.last_error[:500]} if view and view.last_error else {}),
        }
    if semantic_status in {"unknown", "unavailable"}:
        snapshot = await included_tenant_snapshot_state(workspace, coverage)
        if snapshot == "in_progress":
            return {
                **base,
                "status": "recovering",
                "recovery_action": None,
                "message": "Waiting for source data to finish loading before rebuilding the data model.",
            }
        needs_reload = snapshot == "unsafe"
        return {
            **base,
            "status": "needs_materialization" if needs_reload else "needs_semantic_rebuild",
            "recovery_action": "materialization" if needs_reload else "semantic_rebuild",
            "message": (
                "The previous data load did not complete successfully. Restore data before rebuilding its model."
                if needs_reload
                else "Workspace data is available, but its data model needs rebuilding."
            ),
            **({"detail": semantic_error[:500]} if semantic_error else {}),
        }
    return {
        **base,
        "status": "ready",
        "queryable": True,
        "recovery_action": None,
        "message": "Artifact data is ready.",
        **({"detail": semantic_error[:500]} if semantic_error else {}),
    }
