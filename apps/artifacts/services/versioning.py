"""Read-only revision selection for the workspace artifact library."""

from datetime import datetime
from uuid import UUID

from apps.artifacts.models import Artifact, ArtifactType


def latest_visible_version_ids(workspace_id: UUID) -> list[UUID]:
    """Select heads before search, falling back when a revision is soft-deleted.

    Deleted/unsupported ancestors still connect visible revisions. Load only
    lineage metadata, never historical code/data or out-of-workspace parents.
    """
    artifacts = {
        artifact.id: artifact
        for artifact in (
            Artifact.all_objects.filter(workspace_id=workspace_id)
            .order_by()
            .only(
                "id",
                "parent_artifact_id",
                "version",
                "is_deleted",
                "artifact_type",
                "created_at",
                "updated_at",
            )
        )
    }
    roots: dict[UUID, UUID] = {}
    latest_by_root: dict[UUID, Artifact] = {}
    for artifact in artifacts.values():
        if artifact.is_deleted or artifact.artifact_type not in ArtifactType.values:
            continue
        root_id = _lineage_root_id(artifact.id, artifacts, roots)
        current = latest_by_root.get(root_id)
        if current is None or _version_key(artifact) > _version_key(current):
            latest_by_root[root_id] = artifact
    return [artifact.id for artifact in latest_by_root.values()]


def _lineage_root_id(
    artifact_id: UUID, artifacts: dict[UUID, Artifact], roots: dict[UUID, UUID]
) -> UUID:
    path: list[UUID] = []
    positions: dict[UUID, int] = {}
    current_id = artifact_id
    while current_id not in roots:
        if current_id in positions:
            # Corrupt cycles share one stable root and cannot hang the library.
            root_id = min(path[positions[current_id] :])
            break
        positions[current_id] = len(path)
        path.append(current_id)
        parent_id = artifacts[current_id].parent_artifact_id
        if parent_id is None or parent_id not in artifacts:
            root_id = current_id
            break
        current_id = parent_id
    else:
        root_id = roots[current_id]
    for node_id in path:
        roots[node_id] = root_id
    return root_id


def _version_key(artifact: Artifact) -> tuple[int, datetime, datetime, UUID]:
    return (artifact.version or 1, artifact.updated_at, artifact.created_at, artifact.id)
