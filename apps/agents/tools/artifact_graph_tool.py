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
    manifest_entry_summary,
    sync_artifact_semantic_query_manifest,
)
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.chat.artifact_links import link_artifact_to_thread
from apps.chat.models import ThreadArtifact
from apps.workspaces.access import (
    aworkspace_read_allowed,
    aworkspace_write_allowed,
    tool_read_denied,
    tool_write_denied,
)

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


class ArtifactWriteInput(BaseModel):
    action: str = Field(description="One of: create, replace, apply, check.")
    artifact_id: str | None = Field(default=None)
    title: str | None = None
    description: str | None = Field(
        default=None,
        description=(
            "Library-card description, separate from story_doc.prd. Supported on create, "
            "replace, and apply. Omit or use null to preserve it on edits; use an empty "
            "string to clear it. A description-only apply may omit ops."
        ),
    )
    story_doc: dict[str, Any] | None = None
    ops: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Atomic apply operations. Supported shapes: "
            "{op:'set', target:'story/name'|'story/prd'|'story/tags'|"
            "'block/<id>/<slash-delimited-path>', value:any}; "
            "{op:'add_block', after:'start'|'end'|'<id>', block:{...}}; "
            "{op:'remove_block', id:'<id>'}; "
            "{op:'move_block', id:'<id>', after:'start'|'end'|'<id>'}."
        ),
    )
    run_check: bool = Field(
        default=True,
        description=(
            "Deprecated and ignored. New artifacts and document changes require runtime "
            "validation. Metadata-only edits preserve the document without rerunning queries."
        ),
    )


def create_artifact_graph_tools(
    workspace: Workspace,
    user: User | None,
    conversation_id: str | None = None,
) -> list:
    """Create manager-style graph artifact tools for the parent Scout agent."""

    @tool(args_schema=ArtifactGraphOverviewInput)
    async def artifact_graph_overview(artifact_id: str | None = None) -> dict[str, Any]:
        """Read a graph artifact's full doc, summary, diagnostics, and dependencies."""
        if not await aworkspace_read_allowed(user, workspace.id):
            return tool_read_denied()
        artifact = await _load_graph_artifact(workspace, artifact_id, conversation_id)
        if artifact is None:
            return {"status": "not_found", "message": "No graph artifact found."}
        await link_artifact_to_thread(
            artifact,
            conversation_id or artifact.conversation_id,
            workspace,
            source=ThreadArtifact.Source.MENTIONED,
        )
        doc = story_doc_from_artifact_data(artifact.data)
        diagnostics = validate_doc(doc)
        manifest = build_semantic_query_manifest(doc)
        return {
            "status": "ok",
            "artifact": _artifact_summary(artifact),
            "doc": _doc_summary(doc),
            "story_doc": doc,
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
        if not await aworkspace_read_allowed(user, workspace.id):
            return tool_read_denied()
        artifact = await _load_graph_artifact(workspace, artifact_id, conversation_id=None)
        if artifact is None:
            return {"status": "not_found", "message": "Graph artifact not found."}
        await link_artifact_to_thread(
            artifact,
            conversation_id or artifact.conversation_id,
            workspace,
            source=ThreadArtifact.Source.MENTIONED,
        )
        # Inspection derives the current dependencies without rewriting shared
        # live-query metadata or repairing its persisted dependency cache.
        manifest = build_semantic_query_manifest(story_doc_from_artifact_data(artifact.data))
        clean_limit = max(1, min(int(limit or 50), 100))
        clean_offset = max(0, int(offset or 0))
        entries = sorted(manifest["entries"], key=lambda entry: entry["key"])
        total_count = len(entries)
        page = entries[clean_offset : clean_offset + clean_limit]
        persisted = {
            row.query_key: row
            async for row in ArtifactSemanticQuery.objects.filter(
                artifact=artifact, query_key__in=[entry["key"] for entry in page]
            )
        }
        return {
            "status": "ok",
            "artifact": _artifact_summary(artifact),
            "semantic_queries": [
                _derived_semantic_query_record(entry, persisted.get(entry["key"])) for entry in page
            ],
            "pagination": {
                "limit": clean_limit,
                "offset": clean_offset,
                "total_count": total_count,
                "has_more": clean_offset + len(page) < total_count,
            },
            "manifest": _manifest_summary(manifest),
        }

    @tool(args_schema=ArtifactWriteInput)
    async def artifact_write(
        action: str,
        artifact_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
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
        if not await aworkspace_write_allowed(user, workspace.id):
            return tool_write_denied()

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
                    run_check=True,
                )
            if normalized_action == "replace":
                return await _replace_graph_artifact(
                    workspace,
                    user,
                    conversation_id=conversation_id,
                    artifact_id=artifact_id,
                    title=title,
                    description=description,
                    story_doc=story_doc,
                    run_check=True,
                )
            if normalized_action == "apply":
                return await _apply_graph_ops(
                    workspace,
                    user,
                    conversation_id=conversation_id,
                    artifact_id=artifact_id,
                    title=title,
                    description=description,
                    ops=ops,
                    run_check=True,
                )
            if normalized_action == "check":
                artifact = await _load_graph_artifact(workspace, artifact_id, conversation_id)
                if artifact is None:
                    return {"status": "error", "message": "Graph artifact not found."}
                await link_artifact_to_thread(
                    artifact,
                    conversation_id or artifact.conversation_id,
                    workspace,
                    source=ThreadArtifact.Source.MENTIONED,
                )
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
            logger.exception("artifact_write failed for workspace %s", workspace.id)
            return {"status": "error", "message": f"Artifact write failed: {exc}"}
        return {
            "status": "error",
            "message": "Unsupported action. Use create, replace, apply, or check.",
        }

    artifact_graph_overview.name = "artifact_graph_overview"
    get_artifact_semantic_queries.name = "get_artifact_semantic_queries"
    artifact_write.name = "artifact_write"
    return [artifact_graph_overview, get_artifact_semantic_queries, artifact_write]


async def _create_graph_artifact(
    workspace: Workspace,
    user: User | None,
    conversation_id: str | None,
    *,
    title: str | None,
    description: str | None,
    story_doc: dict[str, Any] | None,
    run_check: bool,
) -> dict[str, Any]:
    clean_title = (title or "").strip()
    if not clean_title:
        return {"status": "error", "message": "title is required for create."}
    if story_doc is None:
        return {"status": "error", "message": "story_doc is required for create."}
    doc = normalize_doc(story_doc, name=clean_title)
    diagnostics = validate_doc(doc)
    if diagnostics_have_errors(diagnostics):
        return {
            "status": "error",
            "message": "Graph doc has validation errors.",
            "diagnostics": diagnostics,
        }
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
    result = await _write_result("created", artifact, diagnostics, run_check, user)
    if result["status"] == "created":
        await link_artifact_to_thread(
            artifact,
            conversation_id,
            workspace,
            source=ThreadArtifact.Source.CREATED,
        )
    return result


async def _replace_graph_artifact(
    workspace: Workspace,
    user: User | None,
    *,
    conversation_id: str | None,
    artifact_id: str | None,
    title: str | None,
    description: str | None,
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
        return {
            "status": "error",
            "message": "Graph doc has validation errors.",
            "diagnostics": diagnostics,
        }
    new_artifact = await _create_graph_version(
        original,
        user,
        title=(title.strip() if title else original.title),
        description=description,
        story_doc=doc,
        conversation_id=conversation_id or original.conversation_id,
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(new_artifact)
    result = await _write_result("replaced", new_artifact, diagnostics, run_check, user, original)
    if result["status"] == "replaced":
        await link_artifact_to_thread(
            new_artifact,
            conversation_id or new_artifact.conversation_id,
            workspace,
            source=ThreadArtifact.Source.UPDATED,
        )
    return result


async def _apply_graph_ops(
    workspace: Workspace,
    user: User | None,
    *,
    conversation_id: str | None,
    artifact_id: str | None,
    title: str | None,
    description: str | None,
    ops: list[dict[str, Any]] | None,
    run_check: bool,
) -> dict[str, Any]:
    if not artifact_id:
        return {"status": "error", "message": "artifact_id is required for apply."}
    if not ops and description is None:
        return {"status": "error", "message": "ops or description are required for apply."}
    original = await Artifact.objects.aget(id=artifact_id, workspace=workspace)
    if original.artifact_type != ArtifactType.STORY:
        return {"status": "error", "message": "Only story artifacts can be edited."}
    doc = story_doc_from_artifact_data(original.data)
    updated_doc = apply_ops(doc, ops or [])
    diagnostics = validate_doc(updated_doc)
    if diagnostics_have_errors(diagnostics):
        return {
            "status": "error",
            "message": "Graph doc has validation errors.",
            "diagnostics": diagnostics,
        }
    new_artifact = await _create_graph_version(
        original,
        user,
        title=(title.strip() if title else original.title),
        description=description,
        story_doc=updated_doc,
        conversation_id=conversation_id or original.conversation_id,
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(new_artifact)
    result = await _write_result("updated", new_artifact, diagnostics, run_check, user, original)
    if result["status"] == "updated":
        await link_artifact_to_thread(
            new_artifact,
            conversation_id or new_artifact.conversation_id,
            workspace,
            source=ThreadArtifact.Source.UPDATED,
        )
    return result


async def _create_graph_version(
    original: Artifact,
    user: User | None,
    *,
    title: str,
    description: str | None,
    story_doc: dict[str, Any],
    conversation_id: str,
) -> Artifact:
    artifact = Artifact(
        workspace_id=original.workspace_id,
        created_by=user,
        title=title,
        description=original.description if description is None else description.strip(),
        artifact_type=ArtifactType.STORY,
        code="",
        data={"story_doc": story_doc},
        version=original.version + 1,
        parent_artifact=original,
        conversation_id=conversation_id,
        source_queries=[],
        semantic_queries=[],
        semantic_query_manifest={},
    )
    await artifact.asave()
    return artifact


async def _write_result(
    status: str,
    artifact: Artifact,
    diagnostics: list[dict[str, Any]],
    run_check: bool,
    user: User | None,
    previous: Artifact | None = None,
) -> dict[str, Any]:
    runtime = None
    # Compare persisted content, not the requested action or agent-supplied
    # flags: a replace/apply can include a description AND a query change.
    metadata_only = previous is not None and artifact.data == previous.data
    if run_check and artifact.workspace_id and not metadata_only:
        runtime = await check_graph_artifact(artifact, user_id=str(user.id) if user else "")
    if runtime and runtime.get("success") is False:
        await ThreadArtifact.objects.filter(artifact=artifact).adelete()
        await sync_to_async(artifact.soft_delete, thread_sensitive=True)(user)
        return {
            "status": "error",
            "message": "Graph artifact failed runtime validation and was not published.",
            "artifact": _artifact_summary(artifact),
            "previous_artifact_id": str(previous.id) if previous else None,
            "diagnostics": diagnostics,
            "manifest": _manifest_summary(artifact.semantic_query_manifest or {}),
            "runtime": runtime,
        }
    return {
        "status": status,
        "artifact": _artifact_summary(artifact),
        "previous_artifact_id": str(previous.id) if previous else None,
        "diagnostics": diagnostics,
        "manifest": _manifest_summary(artifact.semantic_query_manifest or {}),
        "runtime": runtime,
        "runtime_validation": "not_required_metadata_only" if metadata_only else "performed",
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
        artifact = (
            await queryset.filter(conversation_id=conversation_id).order_by("-created_at").afirst()
        )
        if artifact:
            return artifact
    return await queryset.order_by("-created_at").afirst()


def _artifact_summary(artifact: Artifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "title": artifact.title,
        "description": artifact.description,
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


def _derived_semantic_query_record(
    entry: dict[str, Any], persisted: ArtifactSemanticQuery | None
) -> dict[str, Any]:
    record = manifest_entry_summary(entry)
    # A derived or stale entry has no persisted identity/timestamps. Retain
    # existing metadata only when that row describes exactly this dependency.
    if persisted is not None and any(
        getattr(persisted, field) != value for field, value in record.items()
    ):
        persisted = None
    return {
        **record,
        "id": str(persisted.id) if persisted else None,
        "created_at": persisted.created_at.isoformat() if persisted else None,
        "updated_at": persisted.updated_at.isoformat() if persisted else None,
    }


__all__ = ["create_artifact_graph_tools"]
