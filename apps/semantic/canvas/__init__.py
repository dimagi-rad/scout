"""Thread-bound semantic canvas: a changeset over the persisted semantic model.

Per-object delta rows, derived states, live diagnostics, an atomic commit
into the semantic tables, and a revision history that can undo each commit.
"""

from apps.semantic.canvas.commit import commit_canvas, undo_revision
from apps.semantic.canvas.history import RevisionUndoError, list_revisions
from apps.semantic.canvas.projections import canvas_projection, render_projection_text
from apps.semantic.canvas.service import (
    CanvasOperationError,
    apply_operations,
    resolve_thread_canvas,
)

__all__ = [
    "CanvasOperationError",
    "RevisionUndoError",
    "apply_operations",
    "canvas_projection",
    "commit_canvas",
    "list_revisions",
    "render_projection_text",
    "resolve_thread_canvas",
    "undo_revision",
]
