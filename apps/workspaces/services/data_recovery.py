"""Workspace query-surface health used by artifact recovery.

The physical data, semantic catalog, and Cube schema have separate lifecycles.
This module turns those implementation details into one small state machine so
artifact pages can decide whether they are safe to render and which repair is
actually required.
"""

from __future__ import annotations

from typing import Any

from apps.chat.models import ThreadJob
from apps.semantic.models import CubeSchema, SemanticModel
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceDataRecovery,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


async def workspace_query_surface(workspace) -> dict[str, Any]:
    """Describe whether ``workspace`` currently has a queryable data surface.

    ``recovery_action`` intentionally names the least expensive repair. Missing
    physical data requires materialization (which also rebuilds semantics),
    while an intact physical schema with no active semantic surface only needs
    the semantic model/Cube build.
    """
    tenant_ids = [
        tenant_id
        async for tenant_id in WorkspaceTenant.objects.filter(workspace=workspace).values_list(
            "tenant_id", flat=True
        )
    ]
    if not tenant_ids:
        return {
            "status": "unavailable",
            "recovery_action": None,
            "physical_status": "missing",
            "semantic_status": "unknown",
            "message": "This workspace has no data sources to restore.",
        }

    physical_ready = False
    physical_status = "missing"
    physical_error = ""
    if len(tenant_ids) == 1:
        queryable_schema = await (
            TenantSchema.objects.filter(
                tenant_id=tenant_ids[0],
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            )
            .order_by("-created_at")
            .afirst()
        )
        physical_ready = queryable_schema is not None
        if queryable_schema is not None:
            physical_status = queryable_schema.state
        else:
            latest_schema = await (
                TenantSchema.objects.filter(tenant_id=tenant_ids[0])
                .order_by("-created_at")
                .afirst()
            )
            if latest_schema is not None:
                physical_status = latest_schema.state
    else:
        view_schema = await WorkspaceViewSchema.objects.filter(workspace=workspace).afirst()
        physical_ready = view_schema is not None and view_schema.state == SchemaState.ACTIVE
        if view_schema is not None:
            physical_status = view_schema.state
            physical_error = view_schema.last_error or ""

    if not physical_ready:
        return {
            "status": "needs_materialization",
            "recovery_action": WorkspaceDataRecovery.RecoveryType.MATERIALIZATION,
            "physical_status": physical_status,
            "semantic_status": "blocked",
            "message": (
                "The data behind this artifact is no longer available. "
                "Restore the workspace data to use it again."
            ),
            **({"detail": physical_error[:500]} if physical_error else {}),
        }

    model = await SemanticModel.objects.filter(workspace=workspace).afirst()
    if model is None:
        return {
            "status": "needs_semantic_rebuild",
            "recovery_action": WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD,
            "physical_status": physical_status,
            "semantic_status": "missing",
            "message": (
                "The workspace data is available, but its semantic model needs to be rebuilt "
                "before this artifact can query it."
            ),
        }

    has_active_cube = await CubeSchema.objects.filter(
        workspace=workspace,
        semantic_model=model,
        status=CubeSchema.Status.ACTIVE,
    ).aexists()
    if model.status != SemanticModel.Status.ACTIVE or not has_active_cube:
        last_build = (model.metadata or {}).get("last_build") or {}
        error = str(last_build.get("error") or "")
        return {
            "status": "needs_semantic_rebuild",
            "recovery_action": WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD,
            "physical_status": physical_status,
            "semantic_status": "unavailable",
            "message": (
                "The workspace data is available, but its semantic model needs to be rebuilt "
                "before this artifact can query it."
            ),
            **({"detail": error[:500]} if error else {}),
        }

    last_build = (model.metadata or {}).get("last_build") or {}
    semantic_status = "ready" if not last_build or last_build.get("ok", True) else "stale"
    return {
        "status": "ready",
        "recovery_action": None,
        "physical_status": physical_status,
        "semantic_status": semantic_status,
        "message": "Artifact data is ready.",
    }


async def artifact_data_state(artifact) -> dict[str, Any]:
    """Return the user-facing recovery state for one artifact."""
    if not artifact.semantic_queries:
        return {
            "status": "not_required",
            "recovery_action": None,
            "physical_status": "not_required",
            "semantic_status": "not_required",
            "message": "This artifact does not query workspace data.",
            "can_retry": False,
            "recovery": None,
        }

    surface = await workspace_query_surface(artifact.workspace)
    if surface["status"] == "ready":
        return {**surface, "can_retry": False, "recovery": None}

    active_recovery = await (
        WorkspaceDataRecovery.objects.filter(
            workspace=artifact.workspace,
            state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
        )
        .order_by("-created_at")
        .afirst()
    )
    if active_recovery is not None:
        return {
            **surface,
            "status": "recovering",
            "message": _recovering_message(active_recovery.recovery_type),
            "can_retry": False,
            "recovery": await _serialize_recovery(active_recovery),
        }

    # ThreadJob spans the whole chat materialization task, including the view
    # and semantic rebuild after per-tenant MaterializationRuns become terminal.
    # That closes the small post-load race where an artifact could otherwise
    # dispatch a duplicate semantic build while chat is still promoting Cube.
    active_chat_materialization = await ThreadJob.objects.filter(
        thread__workspace=artifact.workspace,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        state__in=list(ThreadJob.ACTIVE_STATES),
    ).aexists()
    if active_chat_materialization:
        return {
            **surface,
            "status": "recovering",
            "message": "Workspace data is already being restored in another session.",
            "can_retry": False,
            "recovery": {
                "id": None,
                "type": WorkspaceDataRecovery.RecoveryType.MATERIALIZATION,
                "state": WorkspaceDataRecovery.State.RUNNING,
                "progress": None,
                "created_at": None,
            },
        }

    # A materialization started in chat repairs the same workspace-level data.
    # Detect it so opening an artifact never dispatches a competing pipeline.
    if surface["recovery_action"] == WorkspaceDataRecovery.RecoveryType.MATERIALIZATION:
        active_run = await _active_materialization_run(artifact.workspace)
        if active_run is not None:
            return {
                **surface,
                "status": "recovering",
                "message": "Workspace data is already being restored in another session.",
                "can_retry": False,
                "recovery": {
                    "id": None,
                    "type": WorkspaceDataRecovery.RecoveryType.MATERIALIZATION,
                    "state": WorkspaceDataRecovery.State.RUNNING,
                    "progress": _serialize_progress(active_run.progress),
                    "created_at": active_run.started_at.isoformat(),
                },
            }

    latest_recovery = await (
        WorkspaceDataRecovery.objects.filter(workspace=artifact.workspace)
        .order_by("-created_at")
        .afirst()
    )
    if (
        latest_recovery is not None
        and latest_recovery.state == WorkspaceDataRecovery.State.FAILED
        and latest_recovery.recovery_type == surface["recovery_action"]
    ):
        return {
            **surface,
            "status": "failed",
            "message": "Scout could not restore this artifact's data.",
            **({"detail": latest_recovery.error[:500]} if latest_recovery.error else {}),
            "can_retry": True,
            "recovery": await _serialize_recovery(latest_recovery),
        }

    return {
        **surface,
        "can_retry": surface["recovery_action"] is not None,
        "recovery": None,
    }


async def _active_materialization_run(workspace):
    tenant_ids = WorkspaceTenant.objects.filter(workspace=workspace).values("tenant_id")
    return await (
        MaterializationRun.objects.filter(
            tenant_schema__tenant_id__in=tenant_ids,
            state__in=list(MaterializationRun.ACTIVE_STATES),
        )
        .order_by("-started_at")
        .afirst()
    )


async def _serialize_recovery(recovery: WorkspaceDataRecovery) -> dict[str, Any]:
    progress = None
    if recovery.procrastinate_job_id is not None:
        run = await (
            MaterializationRun.objects.filter(
                procrastinate_job_id=recovery.procrastinate_job_id,
                state__in=list(MaterializationRun.ACTIVE_STATES),
            )
            .order_by("-started_at")
            .afirst()
        )
        if run is not None:
            progress = _serialize_progress(run.progress)
    return {
        "id": str(recovery.id),
        "type": recovery.recovery_type,
        "state": recovery.state,
        "progress": progress,
        "created_at": recovery.created_at.isoformat(),
        "started_at": recovery.started_at.isoformat() if recovery.started_at else None,
        "completed_at": recovery.completed_at.isoformat() if recovery.completed_at else None,
    }


def _serialize_progress(progress: dict | None) -> dict[str, Any] | None:
    if not progress:
        return None
    rows_loaded = progress.get("rows_loaded") or 0
    rows_total = progress.get("rows_total")
    percent = None
    if isinstance(rows_total, int) and rows_total > 0:
        percent = min(100, int(100 * rows_loaded / rows_total))
    return {
        "percent": percent,
        "rows_loaded": rows_loaded,
        "rows_total": rows_total,
        "unit": progress.get("unit") or "rows",
        "message": progress.get("message"),
        "source": progress.get("source"),
        "step": progress.get("step"),
        "total_steps": progress.get("total_steps"),
    }


def _recovering_message(recovery_type: str) -> str:
    if recovery_type == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD:
        return "Rebuilding the semantic model for this artifact."
    return "Restoring the workspace data for this artifact."
