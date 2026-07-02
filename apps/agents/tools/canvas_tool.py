"""Local agent tools for the thread-bound semantic canvas.

Three tools share one service layer with the REST API (single write path):
``canvas_read`` (bounded projections), ``canvas_apply`` (atomic op batches),
and ``canvas_commit`` (persist to the semantic model + Cube rebuild).

The parent Scout agent carries only ``canvas_read``; writes are delegated to
the Canvas Manager subagent (see canvas_manager_agent.py) so the apply/diagnose
loop's token churn stays out of the parent's context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from langchain_core.tools import tool

from apps.chat.models import Thread
from apps.semantic.canvas import (
    apply_operations,
    canvas_projection,
    commit_canvas,
    render_projection_text,
    resolve_thread_canvas,
)
from apps.semantic.services.catalog import SemanticCatalogUnavailable
from apps.semantic.services.sample_rows import sample_dataset_rows
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

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
    """Same policy as the canvas REST endpoints: any role above read."""
    if user is None or not getattr(user, "is_authenticated", True):
        return False
    role = (
        WorkspaceMembership.objects.filter(workspace=workspace, user=user)
        .values_list("role", flat=True)
        .first()
    )
    return role is not None and role != WorkspaceRole.READ


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
                "can_commit": result["can_commit"],
                "text": render_projection_text(projection, "all"),
            }

        return await sync_to_async(_apply, thread_sensitive=True)()

    @tool
    async def canvas_commit() -> dict[str, Any]:
        """Persist the canvas changeset to the semantic model in one transaction.

        Blocked while error diagnostics remain. On success the Cube schema is
        rebuilt so new fields/datasets become queryable; committed objects stay
        on the canvas as the thread's working set.
        """

        def _commit() -> dict[str, Any]:
            if not can_write_canvas(workspace, user):
                return {"errors": [FORBIDDEN_ERROR]}
            try:
                canvas = _resolve_canvas_sync(workspace, user, conversation_id)
            except SemanticCatalogUnavailable as exc:
                return {"errors": [{"op_index": 0, "code": "UNAVAILABLE", "message": str(exc)}]}
            return commit_canvas(canvas, user)

        return await sync_to_async(_commit, thread_sensitive=True)()

    return [canvas_read, canvas_sample_rows, canvas_apply, canvas_commit]
