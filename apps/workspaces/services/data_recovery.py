"""Workspace query-surface health used by artifact recovery.

The physical data, semantic catalog, and Cube schema have separate lifecycles.
This module turns those implementation details into one small state machine so
artifact pages can decide whether they are safe to render and which repair is
actually required.
"""

from __future__ import annotations

from typing import Any

from apps.artifacts.models import Artifact
from apps.artifacts.services.query_state import artifact_query_surface
from apps.chat.models import ThreadJob
from apps.common.error_codes import ErrorCode
from apps.workspaces.models import (
    MaterializationRun,
    WorkspaceDataRecovery,
)
from apps.workspaces.services.access_freshness import FRESHNESS_ERROR_CODES
from apps.workspaces.services.failure_guidance import CREDENTIAL_GUIDANCE
from apps.workspaces.services.load_activity import active_runs_for_workspaces
from apps.workspaces.services.query_state import workspace_query_surface

ROLE_DENIED_MESSAGE = (
    "The requesting user no longer has a read-write or manage workspace role. "
    "Ask a workspace member with write access to retry."
)


def workspace_recovery_error(result: dict, surface: dict) -> str:
    """Select the most useful persisted error for an artifact recovery card."""
    if result.get("error_code") == ErrorCode.WORKSPACE_ROLE_INSUFFICIENT:
        # A role denial is not a source failure; don't label it as one.
        return str(result.get("error") or ROLE_DENIED_MESSAGE)[:1000]
    freshness_codes = set(FRESHNESS_ERROR_CODES.values())
    if result.get("status") == "denied" and result.get("error_code") in freshness_codes:
        # A requester whose access could not be confirmed is not a failed source.
        return str(result["error"])[:1000]
    failed = [
        tenant
        for tenant in result.get("tenants") or []
        if isinstance(tenant, dict) and tenant.get("success") is not True
    ]
    unverified = ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE
    if failed and all(tenant.get("error_code") == unverified for tenant in failed):
        # A mid-run checkpoint denial skips the remaining tenants without a
        # run-level status. The credential codes it can also carry are genuine
        # source remedies, so only the verification-only code is re-labelled.
        return str(failed[0].get("error") or "")[:1000]
    # A failed source commonly causes a downstream Cube *skip*, not a Cube
    # failure. Show the source remedy first; never infer auth advice by parsing
    # human/provider error text, or conflate missing credentials with a 403.
    source_errors: dict[str, list[str]] = {}
    for tenant in result.get("tenants") or []:
        if not isinstance(tenant, dict) or tenant.get("success") is True:
            continue
        error = (
            CREDENTIAL_GUIDANCE.get(tenant.get("error_code"))
            or " ".join(str(tenant.get("error") or "").split())[:200]
        )
        if not error or (error not in source_errors and len(source_errors) == 3):
            continue
        name = tenant.get("display_name")
        if not name:
            name = str(tenant.get("tenant") or "Source")
            if tenant.get("provider"):
                name = f"{name} ({tenant['provider']})"
        label = " ".join(str(name).split())[:80]
        labels = source_errors.setdefault(error, [])
        if label not in labels and len(labels) < 3:
            labels.append(label)
    if source_errors:
        # Opposite remedies (reconnect vs restore upstream permissions) must
        # keep their subjects, even when several sources share one diagnosis.
        return (
            "Data source loading failed: "
            + " ".join(f"{', '.join(labels)}: {error}" for error, labels in source_errors.items())
        )[:1000]
    if result.get("error"):
        return str(result["error"])[:1000]
    cube_result = result.get("cube_schema") or {}
    cube_error = cube_result.get("error") or cube_result.get("reason")
    if cube_error:
        return str(cube_error)[:1000]
    if cube_result.get("ok") is False:
        return "The semantic model rebuild did not complete successfully."
    view_error = (result.get("view_schema") or {}).get("error")
    if view_error:
        return str(view_error)[:1000]
    if surface.get("detail"):
        return str(surface["detail"])[:1000]
    return str(surface.get("message") or "Scout could not restore this artifact's data.")[:1000]


async def artifact_data_state(artifact) -> dict[str, Any]:
    """Return the user-facing recovery state for one artifact."""
    if not artifact.semantic_queries:
        return {
            "status": "not_required",
            "recovery_action": None,
            "physical_status": "not_required",
            "semantic_status": "not_required",
            "queryable": True,
            "message": "This artifact does not query workspace data.",
            "can_retry": False,
            "recovery": None,
        }

    surface = await artifact_query_surface(artifact)

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
        and (not surface["queryable"] or latest_recovery.source_id in {None, artifact.id})
    ):
        return {
            **surface,
            "status": "failed",
            "message": (
                "Showing the last available data. The latest repair did not complete."
                if surface["queryable"]
                else "Scout could not restore this artifact's data."
            ),
            **({"detail": latest_recovery.error[:500]} if latest_recovery.error else {}),
            "can_retry": True,
            "recovery": await _serialize_recovery(latest_recovery),
        }

    return {
        **surface,
        "can_retry": surface["recovery_action"] is not None,
        "recovery": None,
    }


async def recovery_query_surface(recovery) -> dict[str, Any]:
    """Use the durable request's artifact even after its page has closed."""
    if recovery.source_id is None:
        # Chat-started rebuilds, and older rows from before artifact-scoped requests.
        return await workspace_query_surface(recovery.workspace)
    artifact = None
    if recovery.source_type == "artifact":
        artifact = (
            await Artifact.objects.select_related("workspace")
            .filter(workspace_id=recovery.workspace_id, id=recovery.source_id)
            .afirst()
        )
    if artifact is None:
        return {
            "status": "unavailable",
            "queryable": False,
            "recovery_action": None,
            "message": "The artifact that requested recovery is no longer available in this workspace.",
        }
    if not artifact.semantic_queries:
        return {"status": "ready", "queryable": True, "recovery_action": None}
    return await artifact_query_surface(artifact)


async def _active_materialization_run(workspace):
    return await active_runs_for_workspaces([workspace.id]).order_by("-started_at").afirst()


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
    if recovery_type == WorkspaceDataRecovery.RecoveryType.VIEW_REBUILD:
        return "Rebuilding the workspace query layer for this artifact."
    if recovery_type == WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD:
        return "Rebuilding the semantic model for this artifact."
    return "Restoring the workspace data for this artifact."
