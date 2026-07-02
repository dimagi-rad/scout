"""Read projections for the canvas: object graph, per-field diffs, diagnostics.

One projection serves both the REST API (structured payload for the panel) and
the agent tools (compact text rendering, bounded and never silently truncated).
"""

from __future__ import annotations

from typing import Any

from apps.semantic.canvas.service import ChangeType, ObjectType, base_and_state
from apps.semantic.models import SemanticCanvas

_HIDDEN_DRAFT_KEYS = {"_validation", "dataset_uuid", "from_dataset_uuid", "to_dataset_uuid"}

STATE_ORDER = {"conflict": 0, "new": 1, "edited": 2, "deleted": 3, "unchanged": 4}


def canvas_projection(canvas: SemanticCanvas) -> dict[str, Any]:
    from apps.semantic.canvas.diagnostics import compute_diagnostics

    changes = list(canvas.changes.all())
    objects: list[dict[str, Any]] = []
    for change in changes:
        _base, state, base_fields = base_and_state(canvas, change)
        draft_fields = {
            key: value for key, value in change.fields.items() if key not in _HIDDEN_DRAFT_KEYS
        }
        if change.change_type == ChangeType.CREATE:
            name = change.fields.get("name", str(change.object_uuid))
            label = change.fields.get("label", "")
            dataset_name = change.fields.get("dataset_name", "")
            diff = {key: {"from": None, "to": value} for key, value in draft_fields.items() if value}
            if change.object_type == ObjectType.CUSTOM_DATASET:
                validation = change.fields.get("_validation") or {}
                draft_fields["columns"] = validation.get("columns", [])
        else:
            name = base_fields.get("name", str(change.object_uuid))
            label = base_fields.get("label", "")
            dataset_name = base_fields.get("dataset", "")
            diff = {
                key: {"from": base_fields.get(key, ""), "to": value}
                for key, value in change.fields.items()
            }
        objects.append(
            {
                "key": f"{change.object_type}/{name}",
                "object_type": change.object_type,
                "object_uuid": str(change.object_uuid),
                "change_type": change.change_type,
                "name": name,
                "label": label,
                "dataset": dataset_name,
                "state": state,
                "summary": _summary(change, state, diff),
                "diff": diff,
                "fields": draft_fields,
                "base": base_fields,
            }
        )

    objects.sort(key=lambda entry: (STATE_ORDER.get(entry["state"], 9), entry["key"]))
    diagnostics = compute_diagnostics(canvas, changes)
    pending = [entry for entry in objects if entry["state"] != "unchanged"]
    has_errors = any(d["severity"] == "error" for d in diagnostics)
    return {
        "canvas": {
            "id": str(canvas.id),
            "thread_id": str(canvas.thread_id),
            "status": canvas.status,
            "committed_at": canvas.committed_at.isoformat() if canvas.committed_at else None,
            "updated_at": canvas.updated_at.isoformat(),
        },
        "objects": objects,
        "diagnostics": diagnostics,
        "pending_count": len(pending),
        "can_commit": bool(pending) and not has_errors,
    }


def _summary(change, state: str, diff: dict) -> str:
    if state == "new":
        kind = {
            ObjectType.FIELD: "new field",
            ObjectType.RELATIONSHIP: "new relationship",
            ObjectType.CUSTOM_DATASET: "new CTE dataset",
        }.get(change.object_type, "new object")
        return kind
    if state == "deleted":
        return "will be removed on save"
    if state == "conflict":
        return "saved object changed underneath this edit"
    if state == "edited":
        changed = ", ".join(sorted(diff))
        return f"{len(diff)} change(s): {changed}" if diff else "edited"
    return "no pending changes"


def render_projection_text(projection: dict[str, Any], selector: str = "graph") -> str:
    """Compact, bounded text rendering for agent tools."""
    objects = projection["objects"]
    diagnostics = projection["diagnostics"]
    lines: list[str] = []

    if selector in {"graph", "all"}:
        if not objects:
            lines.append("Canvas is empty. Use add_existing/create ops to stage changes.")
        for entry in objects:
            suffix = f" ({entry['dataset']})" if entry["dataset"] else ""
            lines.append(f"{entry['key']}{suffix}  [{entry['state']}]  {entry['summary']}")
    if selector in {"diff", "all"}:
        pending = [e for e in objects if e["state"] not in {"unchanged"}]
        if not pending:
            lines.append("Diff: empty")
        for entry in pending:
            lines.append(f"{entry['key']} [{entry['state']}]")
            for key, delta in sorted(entry["diff"].items()):
                lines.append(
                    f"  {key}: {_short(delta.get('from'))} -> {_short(delta.get('to'))}"
                )
    if selector in {"diagnostics", "all"}:
        if not diagnostics:
            lines.append("Diagnostics: clean")
        for diagnostic in diagnostics:
            lines.append(
                f"{diagnostic['severity'].upper()} {diagnostic['code']} "
                f"{diagnostic['object']}{'/' + diagnostic['path'] if diagnostic['path'] else ''}: "
                f"{diagnostic['message']}"
            )
    lines.append(
        f"Pending: {projection['pending_count']} object(s); "
        f"commit {'allowed' if projection['can_commit'] else 'blocked or empty'}."
    )
    return "\n".join(lines)


def _short(value: Any, limit: int = 120) -> str:
    if value is None or value == "":
        return "(empty)"
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
