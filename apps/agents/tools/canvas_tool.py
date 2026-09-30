"""Local agent tools for the thread-bound semantic canvas.

The tools share one service layer with the REST API (single write path):
``canvas_read`` (bounded projections), ``canvas_apply`` (atomic op batches),
``canvas_commit`` (persist to the semantic model + Cube rebuild, recorded as a
revision), and ``canvas_history`` / ``canvas_undo`` over those revisions.

The parent Scout agent carries only ``canvas_read``; writes are delegated to
the Canvas Manager subagent (see canvas_manager_agent.py) so the apply/diagnose
loop's token churn stays out of the parent's context.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from langchain_core.tools import tool

from apps.artifacts.models import Artifact
from apps.chat.models import Thread
from apps.semantic.canvas import (
    RevisionUndoError,
    apply_operations,
    canvas_projection,
    commit_canvas,
    list_revisions,
    render_projection_text,
    resolve_thread_canvas,
    undo_revision,
)
from apps.semantic.models import SemanticCanvasChange, SemanticDataset, SemanticField
from apps.semantic.services.catalog import SemanticCatalogUnavailable
from apps.semantic.services.sample_rows import sample_dataset_rows
from apps.workspaces.access import aworkspace_read_allowed, workspace_write_allowed

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

READ_SELECTORS = {"graph", "diff", "diagnostics", "all"}

FORBIDDEN_ERROR = {
    "op_index": 0,
    "code": "FORBIDDEN",
    "message": "Read-write or manage role required to edit the canvas.",
}


def can_write_canvas(workspace, user) -> bool:
    """Use the central minimum-role authorizer at the mutation boundary."""
    return workspace_write_allowed(user, workspace.id)


def destructive_deletions(canvas) -> list[dict[str, Any]]:
    """Pending deletes the agent may not commit on its own authority.

    Other agent changes are saved without asking because each one is an
    undoable revision (#714); deleting a whole dataset, or a field a saved
    artifact queries, breaks things people rely on, so the user must confirm.
    """
    deletes = list(
        canvas.changes.filter(
            change_type=SemanticCanvasChange.ChangeType.DELETE,
            object_type__in=[
                SemanticCanvasChange.ObjectType.DATASET,
                SemanticCanvasChange.ObjectType.FIELD,
            ],
        )
    )
    if not deletes:
        return []
    artifacts = [
        (artifact.title, json.dumps([artifact.semantic_queries, artifact.semantic_query_manifest]))
        for artifact in Artifact.objects.filter(workspace=canvas.workspace, is_deleted=False).only(
            "title", "semantic_queries", "semantic_query_manifest"
        )
    ]
    deletions = []
    for change in deletes:
        if change.object_type == SemanticCanvasChange.ObjectType.DATASET:
            dataset = SemanticDataset.objects.filter(id=change.object_uuid).first()
            if dataset is None:
                continue
            label = f"dataset/{dataset.name}"
            member = re.compile(rf"(?<![\w.]){re.escape(dataset.name)}\.\w")
        else:
            field = (
                SemanticField.objects.filter(id=change.object_uuid)
                .select_related("dataset")
                .first()
            )
            if field is None:
                continue
            label = f"field/{field.dataset.name}.{field.name}"
            member = re.compile(
                rf"(?<![\w.]){re.escape(field.dataset.name)}\.{re.escape(field.name)}(?!\w)"
            )
        used_by = list(dict.fromkeys(title for title, text in artifacts if member.search(text)))
        if change.object_type == SemanticCanvasChange.ObjectType.DATASET or used_by:
            deletions.append({"object": label, "used_by_artifacts": used_by[:10]})
    return deletions


def _confirmation_required(deletions: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = []
    for deletion in deletions:
        used_by = deletion["used_by_artifacts"]
        usage = f" It is used by: {', '.join(used_by)}." if used_by else ""
        diagnostics.append(
            {
                "severity": "error",
                "code": "CONFIRMATION_REQUIRED",
                "object": deletion["object"],
                "object_uuid": "",
                "path": "",
                "message": (
                    f"Deleting {deletion['object']} needs the user's explicit confirmation."
                    f"{usage} Nothing was saved. Ask the user; only after they confirm, "
                    "commit again with this object in confirmed_deletions."
                ),
            }
        )
    return {
        "committed": [],
        "blocked": True,
        "conflicts": [],
        "blocking_diagnostics": diagnostics,
        "confirmation_required": deletions,
    }


def _resolve_canvas_sync(workspace, user, conversation_id: str):
    close_old_connections()
    thread, _created = Thread.objects.get_or_create(
        id=conversation_id,
        defaults={"workspace": workspace, "user": user},
    )
    if thread.workspace_id != workspace.id:
        raise SemanticCatalogUnavailable("This conversation belongs to another workspace.")
    return resolve_thread_canvas(workspace, thread, user)


def create_canvas_read_tool(workspace: Workspace, user: User | None, conversation_id: str):
    """Read-only canvas projection tool (safe for the parent agent)."""

    @tool
    async def canvas_read(selector: str = "all") -> str:
        """Read the semantic canvas (the thread's draft changes to datasets).

        selector: 'graph' (objects + states), 'diff' (field-level pending
        changes), 'diagnostics' (validation problems), or 'all'.
        """
        if not await aworkspace_read_allowed(user, workspace.id):
            return "Canvas unavailable: workspace access denied."

        def _read() -> str:
            try:
                canvas = _resolve_canvas_sync(workspace, user, conversation_id)
            except SemanticCatalogUnavailable as exc:
                return f"Canvas unavailable: {exc}"
            projection = canvas_projection(canvas)
            chosen = selector if selector in READ_SELECTORS else "all"
            return render_projection_text(projection, chosen)

        return await sync_to_async(_read, thread_sensitive=True)()

    return canvas_read


def create_canvas_tools(workspace: Workspace, user: User | None, conversation_id: str) -> list:
    """The full canvas toolset for the Canvas Manager subagent."""

    canvas_read = create_canvas_read_tool(workspace, user, conversation_id)

    @tool
    async def canvas_sample_rows(
        dataset: str,
        limit: int = 5,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read a bounded semantic-model sample for reasoning.

        Use this when column names/types are not enough to choose labels,
        descriptions, display formats, or currency codes. The dataset must
        already exist in the saved semantic model; pending CTE drafts are
        validated separately by canvas diagnostics. Optional fields may be
        field names or dataset.field members; otherwise visible dimensions are
        sampled.
        """

        if not await aworkspace_read_allowed(user, workspace.id):
            return {"errors": [{"code": "FORBIDDEN", "message": "Workspace read access required."}]}

        def _sample() -> dict[str, Any]:
            try:
                return sample_dataset_rows(workspace, dataset, limit, fields)
            except SemanticCatalogUnavailable as exc:
                return {"errors": [{"code": "UNAVAILABLE", "message": str(exc)}]}
            except Exception as exc:
                logger.exception("canvas_sample_rows failed for workspace %s", workspace.id)
                return {"errors": [{"code": "SAMPLE_FAILED", "message": str(exc)[:500]}]}

        return await sync_to_async(_sample, thread_sensitive=True)()

    @tool
    async def canvas_apply(operations: list[dict]) -> dict[str, Any]:
        """Apply one atomic batch of canvas ops (the ONLY write path).

        Ops: add_existing (pull a dataset onto the canvas), set (edit one
        field of one object), create (field | relationship | custom_dataset),
        delete_object (canvas-created objects only), remove_from_canvas,
        revert_object. Returns applied ops + current diagnostics; on an
        invalid batch returns {"errors": [...]} and writes nothing.
        """

        def _apply() -> dict[str, Any]:
            if not can_write_canvas(workspace, user):
                return {"errors": [FORBIDDEN_ERROR]}
            try:
                canvas = _resolve_canvas_sync(workspace, user, conversation_id)
            except SemanticCatalogUnavailable as exc:
                return {"errors": [{"op_index": 0, "code": "UNAVAILABLE", "message": str(exc)}]}
            result = apply_operations(canvas, operations, user)
            if "errors" in result:
                return result
            projection = canvas_projection(canvas)
            return {
                "applied": result["applied"],
                "diagnostics": result["diagnostics"],
                "pending_count": result["pending_count"],
                "can_commit": result["can_commit"],
                "text": render_projection_text(projection, "all"),
            }

        return await sync_to_async(_apply, thread_sensitive=True)()

    @tool
    async def canvas_commit(confirmed_deletions: list[str] | None = None) -> dict[str, Any]:
        """Persist the canvas changeset to the semantic model in one transaction.

        Blocked while error diagnostics remain. On success the Cube schema is
        rebuilt so new fields/datasets become queryable; committed objects stay
        on the canvas as the thread's working set.

        Deleting a dataset, or a field an artifact uses, is blocked with
        CONFIRMATION_REQUIRED until the user has explicitly confirmed it; then
        pass the confirmed objects (e.g. "dataset/visit_stats") in
        confirmed_deletions.
        """

        def _commit() -> dict[str, Any]:
            if not can_write_canvas(workspace, user):
                return {"errors": [FORBIDDEN_ERROR]}
            try:
                canvas = _resolve_canvas_sync(workspace, user, conversation_id)
            except SemanticCatalogUnavailable as exc:
                return {"errors": [{"op_index": 0, "code": "UNAVAILABLE", "message": str(exc)}]}
            confirmed = set(confirmed_deletions or [])
            unconfirmed = [
                deletion
                for deletion in destructive_deletions(canvas)
                if deletion["object"] not in confirmed
            ]
            if unconfirmed:
                return _confirmation_required(unconfirmed)
            return commit_canvas(canvas, user)

        return await sync_to_async(_commit, thread_sensitive=True)()

    @tool
    async def canvas_history(limit: int = 10) -> dict[str, Any]:
        """List recent saved data model changes, newest first.

        Each revision has an id, a summary, who made it, and whether it was
        already undone. Use the id with `canvas_undo`.
        """
        if not await aworkspace_read_allowed(user, workspace.id):
            return {"errors": [{"code": "FORBIDDEN", "message": "Workspace read access required."}]}

        def _history() -> dict[str, Any]:
            close_old_connections()
            return {"revisions": list_revisions(workspace, limit=max(1, min(limit, 50)))}

        return await sync_to_async(_history, thread_sensitive=True)()

    @tool
    async def canvas_undo(revision_id: str) -> dict[str, Any]:
        """Undo one saved data model revision, restoring what it changed.

        Refused without writing anything if a later change touched the same
        objects; undo the later revision first. The undo is itself a revision.
        """

        def _undo() -> dict[str, Any]:
            if not can_write_canvas(workspace, user):
                return {"errors": [FORBIDDEN_ERROR]}
            try:
                revision_uuid = uuid.UUID(str(revision_id))
            except ValueError:
                return {"errors": [{"code": "NOT_FOUND", "message": "Unknown revision id."}]}
            thread_id = (
                Thread.objects.filter(id=conversation_id, workspace=workspace)
                .values_list("id", flat=True)
                .first()
            )
            try:
                return undo_revision(workspace, revision_uuid, user, thread_id=thread_id)
            except RevisionUndoError as exc:
                return {"errors": [exc.as_dict()]}

        return await sync_to_async(_undo, thread_sensitive=True)()

    return [
        canvas_read,
        canvas_sample_rows,
        canvas_apply,
        canvas_commit,
        canvas_history,
        canvas_undo,
    ]
