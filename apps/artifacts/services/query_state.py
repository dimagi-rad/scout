"""Artifact query readiness: the shared semantic verdict plus the artifact's repair history."""

from __future__ import annotations

from typing import Any

from django.utils.dateparse import parse_datetime

from apps.semantic.services.query_readiness import query_surface_readiness
from apps.workspaces.models import WorkspaceDataRecovery


async def artifact_query_surface(artifact) -> dict[str, Any]:
    readiness = await query_surface_readiness(artifact.workspace, artifact.semantic_queries)
    surface, model = readiness.surface, readiness.model
    if not (
        readiness.complete
        and model is not None
        and surface["queryable"]
        and artifact.id is not None
    ):
        return surface
    previous = (
        await WorkspaceDataRecovery.objects.filter(
            workspace=artifact.workspace,
            source_type="artifact",
            source_id=artifact.id,
            state__in=[
                WorkspaceDataRecovery.State.COMPLETED,
                WorkspaceDataRecovery.State.FAILED,
            ],
        )
        .order_by("-created_at")
        .afirst()
    )
    if previous is not None and previous.state == WorkspaceDataRecovery.State.FAILED:
        # A failed dispatch/provider attempt may never reach the Cube build
        # recorder. Only a later verified build, not catalog updated_at,
        # proves that this old failure has been repaired by another session.
        last_build = (model.metadata or {}).get("last_build") or {}
        verified_at = parse_datetime(str(last_build.get("at") or ""))
        repaired = (
            last_build.get("ok") is True
            and verified_at is not None
            and verified_at.tzinfo is not None
            and verified_at >= (previous.completed_at or previous.started_at or previous.created_at)
        )
        if not repaired:
            return {
                **surface,
                "recovery_action": previous.recovery_type,
                "message": "Showing the last available data. The latest repair did not complete.",
                "detail": previous.error[:500],
            }
    return surface
