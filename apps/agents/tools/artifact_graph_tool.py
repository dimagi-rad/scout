"""Manager-style tools for semantic graph artifacts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from apps.artifacts.models import Artifact, ArtifactSemanticQuery, ArtifactType
from apps.artifacts.services.graph_doc import (
    GraphDocError,
    apply_ops,
    diagnostics_have_errors,
    normalize_doc,
    story_doc_from_artifact_data,
    validate_doc,
)
from apps.artifacts.services.graph_manifest import (
    build_semantic_query_manifest,
    sync_artifact_semantic_query_manifest,
)
from apps.artifacts.services.graph_runtime import check_graph_artifact

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


class ArtifactGraphOverviewInput(BaseModel):
    artifact_id: str | None = Field(default=None)


class ArtifactSemanticQueriesInput(BaseModel):
    artifact_id: str = Field(description="Story/graph artifact ID.")
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ArtifactGraphManagerInput(BaseModel):
    action: str = Field(description="One of: create, replace, apply, check.")
    artifact_id: str | None = Field(default=None)
    title: str | None = None
    description: str = ""
    story_doc: dict[str, Any] | None = None
    ops: list[dict[str, Any]] | None = None
    run_check: bool = True


def create_artifact_graph_tools(
    workspace: Workspace,
    user: User | None,
    conversation_id: str | None = None,
) -> list:
    """Create manager-style graph artifact tools for the parent Scout agent."""

    @tool(args_schema=ArtifactGraphOverviewInput)
    async def artifact_graph_overview(artifact_id: str | None = None) -> dict[str, Any]:
        """Read-only summary of a graph artifact's doc, diagnostics, and dependencies."""
        artifact = await _load_graph_artifact(workspace, artifact_id, conversation_id)
        if artifact is None:
            return {"status": "not_found", "message": "No graph artifact found."}
        doc = story_doc_from_artifact_data(artifact.data)
        diagnostics = validate_doc(doc)
        manifest = build_semantic_query_manifest(doc)
        return {
            "status": "ok",
            "artifact": _artifact_summary(artifact),
            "doc": _doc_summary(doc),
            "diagnostics": diagnostics,
            "manifest": _manifest_summary(manifest),
        }

    @tool(args_schema=ArtifactSemanticQueriesInput)
    async def get_artifact_semantic_queries(
        artifact_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read paginated semantic query dependencies for a graph artifact."""
        artifact = await _load_graph_artifact(workspace, artifact_id, conversation_id=None)
        if artifact is None:
            return {"status": "not_found", "message": "Graph artifact not found."}
        await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(artifact)
        clean_limit = max(1, min(int(limit or 50), 100))
        clean_offset = max(0, int(offset or 0))
        queryset = ArtifactSemanticQuery.objects.filter(artifact=artifact).order_by("query_key")
        total_count = await queryset.acount()
        rows = await sync_to_async(list, thread_sensitive=True)(queryset[clean_offset : clean_offset + clean_limit])
        return {
            "status": "ok",
            "artifact": _artifact_summary(artifact),
            "semantic_queries": [_semantic_query_record(row) for row in rows],
            "pagination": {
                "limit": clean_limit,
                "offset": clean_offset,
                "total_count": total_count,
                "has_more": clean_offset + len(rows) < total_count,
            },
            "manifest": _manifest_summary(artifact.semantic_query_manifest or {}),
        }

    @tool(args_schema=ArtifactGraphManagerInput)
    async def artifact_graph_manager(
        action: str,
        artifact_id: str | None = None,
        title: str | None = None,
        description: str = "",
        story_doc: dict[str, Any] | None = None,
        ops: list[dict[str, Any]] | None = None,
        run_check: bool = True,
    ) -> dict[str, Any]:
        """
        Create, replace, edit, or check semantic graph artifacts.

        This is the only write surface for story/graph artifacts. Edits are
        atomic: if validation introduces diagnostics, no new artifact version is
        saved.
        """
        normalized_action = (action or "").strip().lower()
        try:
            if normalized_action == "create":
                return await _create_graph_artifact(
                    workspace,
                    user,
                    conversation_id,
                    title=title,
                    description=description,
                    story_doc=story_doc,
                    run_check=run_check,
                )
            if normalized_action == "replace":
                return await _replace_graph_artifact(
                    workspace,
                    user,
                    artifact_id=artifact_id,
                    title=title,
                    story_doc=story_doc,
                    run_check=run_check,
                )
            if normalized_action == "apply":
                return await _apply_graph_ops(
                    workspace,
                    user,
                    artifact_id=artifact_id,
                    title=title,
                    ops=ops,
                    run_check=run_check,
                )
            if normalized_action == "check":
                artifact = await _load_graph_artifact(workspace, artifact_id, conversation_id)
                if artifact is None:
                    return {"status": "error", "message": "Graph artifact not found."}
                runtime = await check_graph_artifact(
                    artifact,
                    user_id=str(user.id) if user else "",
                )
                return {
                    "status": "checked",
                    "artifact": _artifact_summary(artifact),
                    "runtime": runtime,
                }
        except GraphDocError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.exception("artifact_graph_manager failed for workspace %s", workspace.id)
            return {"status": "error", "message": f"Artifact graph manager failed: {exc}"}
        return {
            "status": "error",
            "message": "Unsupported action. Use create, replace, apply, or check.",
        }

    artifact_graph_overview.name = "artifact_graph_overview"
    get_artifact_semantic_queries.name = "get_artifact_semantic_queries"
    artifact_graph_manager.name = "artifact_graph_manager"
    return [artifact_graph_overview, get_artifact_semantic_queries, artifact_graph_manager]


async def _create_graph_artifact(
    workspace: Workspace,
    user: User | None,
    conversation_id: str | None,
    *,
    title: str | None,
    description: str,
    story_doc: dict[str, Any] | None,
    run_check: bool,
) -> dict[str, Any]:
    clean_title = (title or "").strip()
    if not clean_title:
        return {"status": "error", "message": "title is required for create."}
    doc = normalize_doc(story_doc or {}, name=clean_title)
    diagnostics = validate_doc(doc)
    if diagnostics_have_errors(diagnostics):
        return {"status": "error", "message": "Graph doc has validation errors.", "diagnostics": diagnostics}
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title=clean_title,
        description=description.strip() if description else "",
        artifact_type=ArtifactType.STORY,
        code="",
        data={"story_doc": doc},
        version=1,
        conversation_id=conversation_id or "",
        source_queries=[],
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(artifact)
    return await _write_result("created", artifact, diagnostics, run_check, user)


async def _replace_graph_artifact(
    workspace: Workspace,
    user: User | None,
    *,
    artifact_id: str | None,
    title: str | None,
    story_doc: dict[str, Any] | None,
    run_check: bool,
) -> dict[str, Any]:
    if not artifact_id:
        return {"status": "error", "message": "artifact_id is required for replace."}
    if story_doc is None:
        return {"status": "error", "message": "story_doc is required for replace."}
    original = await Artifact.objects.aget(id=artifact_id, workspace=workspace)
    if original.artifact_type != ArtifactType.STORY:
        return {"status": "error", "message": "Only story artifacts can be replaced."}
    doc = normalize_doc(story_doc, name=title or original.title)
    diagnostics = validate_doc(doc)
    if diagnostics_have_errors(diagnostics):
        return {"status": "error", "message": "Graph doc has validation errors.", "diagnostics": diagnostics}
    new_artifact = await sync_to_async(original.create_new_version, thread_sensitive=True)(
        created_by=user,
        title=(title.strip() if title else original.title),
        code="",
        data={"story_doc": doc},
        semantic_queries=[],
        semantic_query_manifest={},
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(new_artifact)
    return await _write_result("replaced", new_artifact, diagnostics, run_check, user, original)


async def _apply_graph_ops(
    workspace: Workspace,
    user: User | None,
    *,
    artifact_id: str | None,
    title: str | None,
    ops: list[dict[str, Any]] | None,
    run_check: bool,
) -> dict[str, Any]:
    if not artifact_id:
        return {"status": "error", "message": "artifact_id is required for apply."}
    if not ops:
        return {"status": "error", "message": "ops are required for apply."}
    original = await Artifact.objects.aget(id=artifact_id, workspace=workspace)
    if original.artifact_type != ArtifactType.STORY:
        return {"status": "error", "message": "Only story artifacts can be edited."}
    doc = story_doc_from_artifact_data(original.data)
    updated_doc = apply_ops(doc, ops)
    diagnostics = validate_doc(updated_doc)
    new_artifact = await sync_to_async(original.create_new_version, thread_sensitive=True)(
        created_by=user,
        title=(title.strip() if title else original.title),
        code="",
        data={"story_doc": updated_doc},
        semantic_queries=[],
        semantic_query_manifest={},
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(new_artifact)
    return await _write_result("updated", new_artifact, diagnostics, run_check, user, original)


async def _write_result(
    status: str,
    artifact: Artifact,
    diagnostics: list[dict[str, Any]],
    run_check: bool,
    user: User | None,
    previous: Artifact | None = None,
) -> dict[str, Any]:
    runtime = None
    if run_check and artifact.workspace_id:
        runtime = await check_graph_artifact(artifact, user_id=str(user.id) if user else "")
    return {
        "status": status,
        "artifact": _artifact_summary(artifact),
        "previous_artifact_id": str(previous.id) if previous else None,
        "diagnostics": diagnostics,
        "manifest": _manifest_summary(artifact.semantic_query_manifest or {}),
        "runtime": runtime,
        "render_url": f"/api/workspaces/{artifact.workspace_id}/artifacts/{artifact.id}/data/",
    }


async def _load_graph_artifact(
    workspace: Workspace,
    artifact_id: str | None,
    conversation_id: str | None,
) -> Artifact | None:
    queryset = Artifact.objects.filter(workspace=workspace, artifact_type=ArtifactType.STORY)
    if artifact_id:
        try:
            return await queryset.aget(id=artifact_id)
        except Artifact.DoesNotExist:
            return None
    if conversation_id:
        artifact = await queryset.filter(conversation_id=conversation_id).order_by("-created_at").afirst()
        if artifact:
            return artifact
    return await queryset.order_by("-created_at").afirst()


def _artifact_summary(artifact: Artifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "title": artifact.title,
        "version": artifact.version,
        "artifact_type": artifact.artifact_type,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }


def _doc_summary(doc: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_doc(doc)
    blocks = normalized.get("blocks") or []
    return {
        "name": normalized.get("name"),
        "schema_version": normalized.get("schema_version"),
        "block_count": len(blocks),
        "blocks": [
            {
                "id": block.get("id"),
                "type": block.get("type"),
                "hidden": bool(block.get("hidden")),
                "inputs": block.get("inputs") or {},
            }
            for block in blocks
            if isinstance(block, dict)
        ],
    }


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "generated_at": manifest.get("generated_at"),
        "entry_count": len(manifest.get("entries") or []),
        "unresolved_count": len(manifest.get("unresolved") or []),
        "unresolved": manifest.get("unresolved") or [],
    }


def _semantic_query_record(row: ArtifactSemanticQuery) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "query_key": row.query_key,
        "query_hash": row.query_hash,
        "query_type": row.query_type,
        "query_payload": row.query_payload,
        "members": row.members,
        "datasets": row.datasets,
        "dependencies": row.dependencies,
        "block_locations": row.block_locations,
        "validation_status": row.validation_status,
        "unresolved_references": row.unresolved_references,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


__all__ = ["create_artifact_graph_tools"]
