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
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from langchain_core.tools import tool

from apps.artifacts.models import Artifact, ArtifactSemanticQuery
from apps.chat.models import Thread
from apps.semantic.canvas import (
    apply_operations,
    canvas_projection,
    commit_canvas,
    list_revisions,
    render_projection_text,
    resolve_thread_canvas,
    undo_revision,
)
from apps.semantic.canvas.history import changes_definition
from apps.semantic.canvas.objects import FIELD_CURATION_KEYS
from apps.semantic.models import (
    SemanticCanvasChange,
    SemanticDataset,
    SemanticField,
    SemanticModelRevision,
)
from apps.semantic.services.catalog import SemanticCatalogUnavailable
from apps.semantic.services.sample_rows import sample_dataset_rows
from apps.workspaces.access import aworkspace_read_allowed, workspace_write_allowed

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

READ_SELECTORS = {"graph", "diff", "diagnostics", "all"}
# How many later user turns may carry the answer to a deletion question.
CONFIRMATION_WINDOW_TURNS = 3

FORBIDDEN_ERROR = {
    "op_index": 0,
    "code": "FORBIDDEN",
    "message": "Read-write or manage role required to edit the canvas.",
}


def can_write_canvas(workspace, user) -> bool:
    """Use the central minimum-role authorizer at the mutation boundary."""
    return workspace_write_allowed(user, workspace.id)


def destructive_deletions(canvas) -> list[dict[str, Any]]:
    """Pending deletes and renames the agent may not commit on its own authority.

    Other agent changes are saved without asking because each one is an
    undoable revision (#714); deleting a whole dataset, or deleting or renaming
    a field a saved artifact queries, breaks things people rely on, so the user
    must confirm.
    """
    return _needs_confirmation(_pending_impact(canvas))


def _needs_confirmation(impact: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in impact if item.get("change") != "redefine"]


def _pending_impact(canvas) -> list[dict[str, Any]]:
    """Destructive changes, plus ``redefine`` entries for used fields whose meaning changes."""
    changes = canvas.changes.filter(
        object_type__in=[
            SemanticCanvasChange.ObjectType.DATASET,
            SemanticCanvasChange.ObjectType.FIELD,
        ],
        change_type__in=[
            SemanticCanvasChange.ChangeType.DELETE,
            SemanticCanvasChange.ChangeType.UPDATE,
        ],
    )
    targets = []
    for change in changes:
        is_delete = change.change_type == SemanticCanvasChange.ChangeType.DELETE
        if change.object_type == SemanticCanvasChange.ObjectType.DATASET:
            if not is_delete:
                continue
            dataset = SemanticDataset.objects.filter(id=change.object_uuid).first()
            if dataset is not None:
                targets.append((dataset.name, None, "delete"))
            continue
        edited = set(change.fields or {})
        if not is_delete and not edited - FIELD_CURATION_KEYS:
            continue
        field = (
            SemanticField.objects.filter(id=change.object_uuid).select_related("dataset").first()
        )
        if field is None:
            continue
        if is_delete:
            targets.append((field.dataset.name, field.name, "delete"))
            continue
        # One edit can both rename and redefine; the user hears about each.
        if "name" in edited and change.fields["name"] != field.name:
            targets.append((field.dataset.name, field.name, "rename"))
        if edited - FIELD_CURATION_KEYS - {"name"}:
            targets.append((field.dataset.name, field.name, "redefine"))
    return _deletions_needing_confirmation(canvas.workspace, targets)


def _undo_impact(workspace, revision_id) -> list[dict[str, Any]]:
    """Like ``_pending_impact``, for the changes an undo writes: the reverse of each entry."""
    revision = SemanticModelRevision.objects.filter(id=revision_id, workspace=workspace).first()
    targets = []
    for entry in (revision.changes or []) if revision else []:
        before, after = entry.get("before"), entry.get("after")
        if not after:
            continue
        if before is None:
            if entry["object_type"] == "dataset":
                targets.append((after["name"], None, "delete"))
            elif entry["object_type"] == "field":
                targets.append((after["dataset_name"], after["name"], "delete"))
            continue
        if entry["object_type"] != "field":
            continue
        # Artifacts saved since the revision use its after-state names.
        if before.get("name") != after.get("name"):
            targets.append((after["dataset_name"], after["name"], "rename"))
        # A rename alone changes no numbers, as in _pending_impact.
        if changes_definition({**before, "name": after.get("name")}, after):
            targets.append((after["dataset_name"], after["name"], "redefine"))
    return _deletions_needing_confirmation(workspace, targets)


def _deletions_needing_confirmation(workspace, targets) -> list[dict[str, Any]]:
    """``targets`` are ``(dataset, field-or-None, change)``; any dataset qualifies, a field
    only when used. Changes other than ``delete`` are named in a ``change`` key."""
    if not targets:
        return []
    artifacts = _artifact_member_texts(workspace)
    deletions = []
    for dataset_name, field_name, change in targets:
        if field_name is None:
            label = f"dataset/{dataset_name}"
            member = re.compile(rf"(?<![\w.]){re.escape(dataset_name)}\.\w")
        else:
            label = f"field/{dataset_name}.{field_name}"
            member = re.compile(
                rf"(?<![\w.]){re.escape(dataset_name)}\.{re.escape(field_name)}(?!\w)"
            )
        used_by = list(dict.fromkeys(title for title, text in artifacts if member.search(text)))
        if field_name is None or used_by:
            item = {"object": label, "used_by_artifacts": used_by[:10]}
            if change != "delete":
                item["change"] = change
            deletions.append(item)
    return deletions


def _artifact_member_texts(workspace) -> list[tuple[str, str]]:
    """``(title, searchable text)`` per current artifact version.

    Stories saved before manifests existed keep their members only in
    ``data.story_doc``, so it is scanned too, along with the normalized query rows.
    """
    # A failed graph write leaves a soft-deleted child; only a live child supersedes.
    current = (
        Artifact.objects.filter(workspace=workspace, is_deleted=False)
        .exclude(child_versions__is_deleted=False)
        .only("title", "data", "semantic_queries", "semantic_query_manifest")
    )
    members: dict[Any, list] = {}
    for artifact_id, query_members in ArtifactSemanticQuery.objects.filter(
        workspace=workspace
    ).values_list("artifact_id", "members"):
        members.setdefault(artifact_id, []).append(query_members)
    return [
        (
            artifact.title,
            json.dumps(
                [
                    artifact.semantic_queries,
                    artifact.semantic_query_manifest,
                    artifact.data.get("story_doc") if isinstance(artifact.data, dict) else None,
                    members.get(artifact.id, []),
                ],
                default=str,
            ),
        )
        for artifact in current
    ]


def _confirmation_required(deletions: list[dict[str, Any]], *, retry: str) -> dict[str, Any]:
    diagnostics = []
    for deletion in deletions:
        used_by = deletion["used_by_artifacts"]
        usage = f" It is used by: {', '.join(used_by)}." if used_by else ""
        verb = "Renaming" if deletion.get("change") == "rename" else "Deleting"
        diagnostics.append(
            {
                "severity": "error",
                "code": "CONFIRMATION_REQUIRED",
                "object": deletion["object"],
                "object_uuid": "",
                "path": "",
                "message": (
                    f"{verb} {deletion['object']} needs the user's explicit confirmation."
                    f"{usage} Nothing was saved. Report this so the user is asked, and end "
                    "the turn: a confirmation counts only if it comes in a later user message, "
                    f"then {retry} again with this object in confirmed_deletions."
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


def _gate_deletions(
    canvas, deletions, confirmed_deletions, human_turn: int | None, *, retry: str
) -> dict[str, Any] | None:
    """Refuse unconfirmed deletions; None lets the write run.

    A label in ``confirmed_deletions`` counts only in one of the few user turns
    after the one where the agent was told to ask, so it cannot ask and confirm
    in one turn or lean on a stale question. That the user said yes still rests
    on the agent's reading of the reply.
    """
    pending = dict(canvas.pending_confirmations or {})
    confirmed = set(confirmed_deletions or [])

    def asked_recently(deletion: dict[str, Any]) -> bool:
        asked_at = pending.get(_question_key(deletion))
        return (
            isinstance(asked_at, int)
            and human_turn is not None
            and human_turn - CONFIRMATION_WINDOW_TURNS <= asked_at <= human_turn
        )

    def accepted(deletion: dict[str, Any]) -> bool:
        return (
            deletion["object"] in confirmed
            and asked_recently(deletion)
            and pending[_question_key(deletion)] < human_turn
        )

    unconfirmed = [deletion for deletion in deletions if not accepted(deletion)]
    if unconfirmed:
        for deletion in deletions:
            # Re-stamping a live question would void the answer the user is about to give.
            if human_turn is not None and not asked_recently(deletion):
                pending[_question_key(deletion)] = human_turn
        canvas.pending_confirmations = pending
        canvas.save(update_fields=["pending_confirmations", "updated_at"])
        return _confirmation_required(unconfirmed, retry=retry)
    return None


def _clear_confirmations(canvas, deletions) -> None:
    """Called only once the write happened, so a blocked retry keeps the user's yes."""
    pending = dict(canvas.pending_confirmations or {})
    if not any(_question_key(deletion) in pending for deletion in deletions):
        return
    for deletion in deletions:
        pending.pop(_question_key(deletion), None)
    canvas.pending_confirmations = pending
    canvas.save(update_fields=["pending_confirmations", "updated_at"])


def _question_key(deletion: dict[str, Any]) -> str:
    """Scope a question to its kind, so a yes to renaming cannot be spent on deleting."""
    return f"{deletion.get('change', 'delete')}:{deletion['object']}"


def _resolve_canvas_sync(workspace, user, conversation_id: str):
    close_old_connections()
    # Never create the row here: chat_view creates it before the turn starts, and a
    # missing row means the thread was deleted, whose checkpoints must not be adopted.
    try:
        thread = Thread.objects.filter(id=conversation_id).first()
    except ValidationError:
        thread = None
    if thread is None:
        raise SemanticCatalogUnavailable("This conversation no longer exists.")
    if thread.workspace_id != workspace.id:
        raise SemanticCatalogUnavailable("This conversation belongs to another workspace.")
    if thread.user_id != getattr(user, "pk", None):
        raise SemanticCatalogUnavailable("This conversation belongs to another user.")
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


def create_canvas_tools(
    workspace: Workspace,
    user: User | None,
    conversation_id: str,
    *,
    human_turn: int | None = None,
) -> list:
    """The full canvas toolset for the Canvas Manager subagent.

    ``human_turn`` is the parent thread's user-turn count; without it no
    deletion can be confirmed.
    """

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

        Deleting a dataset, or deleting or renaming a field an artifact uses,
        is blocked with CONFIRMATION_REQUIRED until the user has explicitly
        confirmed it; then pass the confirmed objects (e.g.
        "dataset/visit_stats") in confirmed_deletions. A saved change to the
        definition of a field an artifact uses is listed in
        redefined_fields_used_by_artifacts.
        """

        def _commit() -> dict[str, Any]:
            if not can_write_canvas(workspace, user):
                return {"errors": [FORBIDDEN_ERROR]}
            try:
                canvas = _resolve_canvas_sync(workspace, user, conversation_id)
            except SemanticCatalogUnavailable as exc:
                return {"errors": [{"op_index": 0, "code": "UNAVAILABLE", "message": str(exc)}]}
            impact = _pending_impact(canvas)
            deletions = _needs_confirmation(impact)
            refusal = _gate_deletions(
                canvas, deletions, confirmed_deletions, human_turn, retry="commit"
            )
            if refusal:
                return refusal
            report = commit_canvas(canvas, user)
            if "revision" in report:
                _clear_confirmations(canvas, deletions)
                if redefined := [item for item in impact if item.get("change") == "redefine"]:
                    report["redefined_fields_used_by_artifacts"] = redefined
            return report

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
    async def canvas_undo(
        revision_id: str, confirmed_deletions: list[str] | None = None
    ) -> dict[str, Any]:
        """Undo one saved data model revision, restoring what it changed.

        Refused without writing anything if a later change touched the same
        objects; undo the later revision first. The undo is itself a revision.
        Undoing a create deletes the object and undoing a rename renames it
        back, so the same CONFIRMATION_REQUIRED rule as canvas_commit applies,
        with the same confirmed_deletions, and redefined_fields_used_by_artifacts
        is reported the same way.
        """

        def _undo() -> dict[str, Any]:
            close_old_connections()
            if not can_write_canvas(workspace, user):
                return {"errors": [FORBIDDEN_ERROR]}
            try:
                revision_uuid = uuid.UUID(str(revision_id))
            except ValueError:
                return {"errors": [{"code": "NOT_FOUND", "message": "Unknown revision id."}]}
            try:
                canvas = _resolve_canvas_sync(workspace, user, conversation_id)
            except SemanticCatalogUnavailable as exc:
                return {"errors": [{"code": "UNAVAILABLE", "message": str(exc)}]}
            impact = _undo_impact(workspace, revision_uuid)
            deletions = _needs_confirmation(impact)
            refusal = _gate_deletions(
                canvas, deletions, confirmed_deletions, human_turn, retry="undo"
            )
            if refusal:
                return refusal
            result = undo_revision(workspace, revision_uuid, user, thread_id=canvas.thread_id)
            if refusal := result.get("refused"):
                return {"errors": [refusal]}
            _clear_confirmations(canvas, deletions)
            if redefined := [item for item in impact if item.get("change") == "redefine"]:
                result["redefined_fields_used_by_artifacts"] = redefined
            return result

        return await sync_to_async(_undo, thread_sensitive=True)()

    return [
        canvas_read,
        canvas_sample_rows,
        canvas_apply,
        canvas_commit,
        canvas_history,
        canvas_undo,
    ]
