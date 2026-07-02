"""Thread-bound semantic canvas: a changeset over the persisted semantic model.

Modeled on the changeset design in docs/canvas-design.md: per-object delta rows,
derived states, live diagnostics, and an atomic commit into the semantic tables.
"""

from apps.semantic.canvas.commit import commit_canvas
from apps.semantic.canvas.projections import canvas_projection, render_projection_text
from apps.semantic.canvas.service import (
    CanvasOperationError,
    apply_operations,
    resolve_thread_canvas,
)

__all__ = [
    "CanvasOperationError",
    "apply_operations",
    "canvas_projection",
    "commit_canvas",
    "render_projection_text",
    "resolve_thread_canvas",
]
