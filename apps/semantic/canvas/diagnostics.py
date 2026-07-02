"""Live validation over the canvas changeset.

Diagnostics are computed on read (and after every apply) against the merged
canvas state. Every diagnostic here is introduced by the changeset itself —
persisted-model defects are not the canvas's problem — so all error-severity
diagnostics gate commit.
"""

from __future__ import annotations

from typing import Any

from apps.semantic.canvas.objects import dataset_column_names
from apps.semantic.canvas.service import (
    ChangeType,
    ObjectType,
    base_and_state,
    custom_dataset_primary_key,
    validate_custom_dataset_draft,
)
from apps.semantic.models import CustomDataset, SemanticCanvasChange, SemanticField


def compute_diagnostics(canvas, changes: list[SemanticCanvasChange] | None = None) -> list[dict]:
    if changes is None:
        changes = list(canvas.changes.all())
    diagnostics: list[dict[str, Any]] = []
    model = canvas.semantic_model

    field_drafts = [
        c for c in changes if c.object_type == ObjectType.FIELD and c.change_type == ChangeType.CREATE
    ]
    relationship_drafts = [
        c
        for c in changes
        if c.object_type == ObjectType.RELATIONSHIP and c.change_type == ChangeType.CREATE
    ]
    custom_drafts = [
        c
        for c in changes
        if c.object_type == ObjectType.CUSTOM_DATASET and c.change_type == ChangeType.CREATE
    ]

    for change in field_drafts:
        diagnostics.extend(_field_draft_diagnostics(model, change, field_drafts))
    for change in relationship_drafts:
        diagnostics.extend(
            _relationship_draft_diagnostics(canvas, model, change, relationship_drafts, field_drafts)
        )
    for change in custom_drafts:
        diagnostics.extend(_custom_draft_diagnostics(canvas, model, change, custom_drafts))

    for change in changes:
        if change.change_type == ChangeType.CREATE:
            continue
        _base, state, _serialized = base_and_state(canvas, change)
        if state == "conflict":
            diagnostics.append(
                _diagnostic(
                    "CONFLICT",
                    change,
                    "",
                    "The saved object changed (or was removed) after this edit was "
                    "drafted. Revert the object to pick up the current version.",
                )
            )
    return diagnostics


def _field_draft_diagnostics(model, change, siblings) -> list[dict]:
    out: list[dict[str, Any]] = []
    fields = change.fields
    dataset = model.datasets.filter(id=fields.get("dataset_uuid")).first()
    if dataset is None:
        out.append(_diagnostic("UNKNOWN_DATASET", change, "dataset", "The target dataset is gone."))
        return out

    name = fields.get("name", "")
    duplicate_persisted = SemanticField.objects.filter(
        dataset=dataset, name=name, is_visible=True
    ).exists()
    duplicate_draft = any(
        other.id != change.id
        and other.fields.get("dataset_uuid") == str(dataset.id)
        and other.fields.get("name") == name
        for other in siblings
    )
    if duplicate_persisted or duplicate_draft:
        out.append(
            _diagnostic(
                "DUPLICATE_FIELD_NAME",
                change,
                "name",
                f"'{dataset.name}.{name}' already exists. Choose a unique field name.",
            )
        )

    expression = fields.get("expression", "")
    measure_type = fields.get("measure_type", "")
    if measure_type == "count":
        return out
    columns = dataset_column_names(dataset)
    if not expression:
        out.append(
            _diagnostic(
                "MISSING_EXPRESSION",
                change,
                "expression",
                "Set expression to one of the dataset's columns"
                + (f" (e.g. {', '.join(sorted(columns)[:5])})." if columns else "."),
            )
        )
    elif expression not in columns:
        out.append(
            _diagnostic(
                "UNKNOWN_COLUMN",
                change,
                "expression",
                f"'{expression}' is not a column on {dataset.name}. Expressions must "
                "name an existing column; for computed logic create a CTE dataset instead.",
            )
        )
    return out


def _relationship_draft_diagnostics(canvas, model, change, siblings, field_drafts) -> list[dict]:
    out: list[dict[str, Any]] = []
    fields = change.fields
    from_dataset = model.datasets.filter(id=fields.get("from_dataset_uuid")).first()
    to_dataset = model.datasets.filter(id=fields.get("to_dataset_uuid")).first()
    if from_dataset is None or to_dataset is None:
        out.append(
            _diagnostic("UNKNOWN_DATASET", change, "", "A linked dataset no longer exists.")
        )
        return out
    if from_dataset.id == to_dataset.id:
        out.append(
            _diagnostic(
                "SELF_RELATIONSHIP", change, "to_dataset", "A dataset cannot link to itself."
            )
        )
    # Cube refuses to compile a join whose owning cube lacks a primary key
    # ("primary key ... is required when join is defined"), so catch it here
    # instead of letting the commit's schema rebuild fail.
    if not from_dataset.primary_key:
        out.append(
            _diagnostic(
                "MISSING_PRIMARY_KEY",
                change,
                "from_dataset",
                f"'{from_dataset.name}' has no primary key, which links require. "
                "Refresh the workspace data to detect it.",
            )
        )

    for path, dataset in (("from_field", from_dataset), ("to_field", to_dataset)):
        field_name = fields.get(path, "")
        exists = SemanticField.objects.filter(
            dataset=dataset, name=field_name, is_visible=True
        ).exists() or any(
            draft.fields.get("dataset_uuid") == str(dataset.id)
            and draft.fields.get("name") == field_name
            for draft in field_drafts
        )
        if not field_name or not exists:
            out.append(
                _diagnostic(
                    "UNKNOWN_FIELD",
                    change,
                    path,
                    f"'{field_name}' is not a field on {dataset.name}.",
                )
            )

    name = fields.get("name", "")
    duplicate = canvas.workspace.semantic_relationships.filter(name=name).exists() or any(
        other.id != change.id and other.fields.get("name") == name for other in siblings
    )
    if duplicate:
        out.append(
            _diagnostic(
                "DUPLICATE_RELATIONSHIP_NAME",
                change,
                "name",
                f"Relationship '{name}' already exists.",
            )
        )
    return out


def _custom_draft_diagnostics(canvas, model, change, siblings) -> list[dict]:
    out: list[dict[str, Any]] = []
    fields = change.fields
    name = fields.get("name", "")
    duplicate = (
        model.datasets.filter(name=name, is_visible=True).exists()
        or CustomDataset.objects.filter(workspace=canvas.workspace, name=name).exists()
        or any(other.id != change.id and other.fields.get("name") == name for other in siblings)
    )
    if duplicate:
        out.append(
            _diagnostic(
                "DUPLICATE_DATASET_NAME",
                change,
                "name",
                f"A dataset named '{name}' already exists.",
            )
        )

    validation = validate_custom_dataset_draft(canvas, change)
    if validation.get("error"):
        out.append(_diagnostic("INVALID_SQL", change, "definition_sql", validation["error"]))
        return out
    columns = validation.get("columns") or []
    if not columns:
        out.append(
            _diagnostic(
                "INVALID_SQL", change, "definition_sql", "The query returns no columns."
            )
        )
        return out

    column_names = {column.get("name") for column in columns}
    primary_key = custom_dataset_primary_key(fields, columns)
    if not primary_key:
        out.append(
            _diagnostic(
                "MISSING_PRIMARY_KEY",
                change,
                "primary_key",
                "Cube needs a primary key on every dataset. Set primary_key to "
                f"one of the query's columns: {', '.join(sorted(str(n) for n in column_names))}.",
            )
        )
    elif primary_key not in column_names:
        out.append(
            _diagnostic(
                "UNKNOWN_COLUMN",
                change,
                "primary_key",
                f"'{primary_key}' is not one of the query's columns.",
            )
        )
    return out


def _diagnostic(code: str, change, path: str, message: str) -> dict[str, Any]:
    name = change.fields.get("name", "") if isinstance(change.fields, dict) else ""
    return {
        "code": code,
        "severity": "error",
        "object": f"{change.object_type}/{name or change.object_uuid}",
        "object_uuid": str(change.object_uuid),
        "path": path,
        "message": message,
    }
