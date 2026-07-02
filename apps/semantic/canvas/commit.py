"""Atomic commit of the canvas changeset into the semantic tables.

Gate: any error diagnostic blocks the whole commit (all canvas diagnostics are
changeset-introduced). Conflicts commit nothing. All planned writes happen in
one transaction with an in-transaction fingerprint re-check; settled rows stay
on the canvas as membership rows so it remains the thread's working set.

The Cube schema rebuild runs after the transaction: a rebuild failure never
rolls back committed semantic rows — the previous ACTIVE schema keeps serving
(see build_and_promote_cube_schema) and the failure is reported in the result.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.semantic.canvas.service import ChangeType, ObjectType, base_and_state
from apps.semantic.models import (
    CustomDataset,
    SemanticCanvasChange,
    SemanticDataset,
    SemanticField,
    SemanticRelationship,
)
from apps.semantic.services.catalog import _sync_fields
from apps.semantic.services.cube_schema import build_and_promote_cube_schema

logger = logging.getLogger(__name__)

CANVAS_SOURCE = "canvas"


class _CommitConflict(Exception):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("canvas commit conflict")
        self.conflicts = conflicts


def commit_canvas(canvas, user=None) -> dict[str, Any]:
    """Persist all pending canvas changes; returns a structured commit report."""
    from apps.semantic.canvas.diagnostics import compute_diagnostics

    changes = list(canvas.changes.all())
    pending = [c for c in changes if _is_pending(canvas, c)]
    if not pending:
        return {"committed": [], "blocked": False, "conflicts": [], "blocking_diagnostics": []}

    diagnostics = compute_diagnostics(canvas, changes)
    blocking = [d for d in diagnostics if d["severity"] == "error" and d["code"] != "CONFLICT"]
    conflicts = [
        _conflict_entry(canvas, c) for c in changes if _state_of(canvas, c) == "conflict"
    ]
    if blocking:
        return {
            "committed": [],
            "blocked": True,
            "blocking_diagnostics": blocking,
            "conflicts": conflicts,
        }
    if conflicts:
        return {
            "committed": [],
            "blocked": False,
            "blocking_diagnostics": [],
            "conflicts": conflicts,
        }

    try:
        committed = _commit_transaction(canvas, pending, user)
    except _CommitConflict as exc:
        return {
            "committed": [],
            "blocked": False,
            "blocking_diagnostics": [],
            "conflicts": exc.conflicts,
        }

    cube_outcome: dict[str, Any]
    try:
        cube_schema = build_and_promote_cube_schema(canvas.workspace, model=canvas.semantic_model)
        cube_outcome = {"ok": True, "content_hash": cube_schema.content_hash}
    except Exception as exc:
        logger.exception(
            "Cube schema rebuild failed after canvas commit for workspace %s",
            canvas.workspace_id,
        )
        cube_outcome = {"ok": False, "error": str(exc)[:500]}

    return {
        "committed": committed,
        "blocked": False,
        "blocking_diagnostics": [],
        "conflicts": [],
        "cube_schema": cube_outcome,
    }


def _is_pending(canvas, change) -> bool:
    return _state_of(canvas, change) not in {"unchanged"}


def _state_of(canvas, change) -> str:
    _base, state, _serialized = base_and_state(canvas, change)
    return state


def _conflict_entry(canvas, change) -> dict[str, Any]:
    return {
        "object": f"{change.object_type}/{change.object_uuid}",
        "object_uuid": str(change.object_uuid),
        "message": "The saved object changed after this edit was drafted.",
    }


def _commit_transaction(canvas, pending: list[SemanticCanvasChange], user) -> list[dict[str, Any]]:
    committed: list[dict[str, Any]] = []
    now = timezone.now()
    with transaction.atomic():
        model = canvas.semantic_model
        workspace = canvas.workspace
        for change in pending:
            # Capture identity before settle/delete clears the draft fields.
            change_type = change.change_type
            name = change.fields.get("name", "") if change.fields else ""
            if change.change_type == ChangeType.CREATE:
                obj = _commit_create(canvas, model, workspace, change, user)
                name = getattr(obj, "name", "") or name
                _settle_as_membership(change, obj)
            elif change.change_type == ChangeType.DELETE:
                name = _commit_delete(canvas, model, workspace, change) or name
                change.delete()
            else:
                obj = _commit_update(canvas, model, workspace, change)
                name = getattr(obj, "name", "") or name
                _settle_as_membership(change, obj)
            committed.append(
                {
                    "object_type": change.object_type,
                    "object_uuid": str(change.object_uuid),
                    "name": name,
                    "change_type": change_type,
                }
            )
        canvas.committed_at = now
        canvas.save(update_fields=["committed_at", "updated_at"])
    return committed


def _locked_base(canvas, change):
    """Re-fetch the base row FOR UPDATE and re-check the fingerprint (TOCTOU)."""
    model_class = {
        ObjectType.DATASET: SemanticDataset,
        ObjectType.FIELD: SemanticField,
        ObjectType.RELATIONSHIP: SemanticRelationship,
    }[SemanticCanvasChange.ObjectType(change.object_type)]
    obj = model_class.objects.select_for_update().filter(id=change.object_uuid).first()
    if obj is None or (change.base_fingerprint and obj.updated_at != change.base_fingerprint):
        raise _CommitConflict([_conflict_entry(canvas, change)])
    return obj


def _commit_update(canvas, model, workspace, change):
    obj = _locked_base(canvas, change)
    curated = set((obj.metadata or {}).get("curated_fields", []))
    for key, value in change.fields.items():
        setattr(obj, key, value)
        curated.add(key)
    metadata = dict(obj.metadata or {})
    metadata["curated_fields"] = sorted(curated)
    obj.metadata = metadata
    obj.save()
    obj.refresh_from_db(fields=["updated_at"])
    return obj


def _commit_delete(canvas, model, workspace, change) -> str:
    obj = _locked_base(canvas, change)
    name = getattr(obj, "name", "")
    if change.object_type == ObjectType.DATASET:
        # Only custom (CTE) datasets reach here (enforced at op time).
        custom = obj.custom_dataset
        obj.delete()
        if custom is not None:
            custom.delete()
    else:
        obj.delete()
    return name


def _commit_create(canvas, model, workspace, change, user):
    fields = change.fields
    if change.object_type == ObjectType.FIELD:
        return _create_field(model, change, user)
    if change.object_type == ObjectType.RELATIONSHIP:
        return _create_relationship(model, workspace, change, user)
    if change.object_type == ObjectType.CUSTOM_DATASET:
        return _create_custom_dataset(canvas, model, workspace, change, user)
    raise ValueError(f"Unexpected create object_type {change.object_type} ({fields}).")


def _create_field(model, change, user):
    fields = change.fields
    dataset = model.datasets.get(id=fields["dataset_uuid"])
    expression = fields.get("expression") or ("*" if fields.get("measure_type") == "count" else "")
    metadata = {"source": CANVAS_SOURCE}
    if user is not None and getattr(user, "id", None):
        metadata["created_by"] = str(user.id)
    return SemanticField.objects.create(
        id=change.object_uuid,
        dataset=dataset,
        name=fields["name"],
        label=fields.get("label", ""),
        description=fields.get("description", ""),
        field_type=fields["field_type"],
        data_type=fields.get("data_type", ""),
        expression=expression,
        measure_type=fields.get("measure_type", ""),
        is_visible=True,
        metadata=metadata,
    )


def _create_relationship(model, workspace, change, user):
    fields = change.fields
    from_dataset = model.datasets.get(id=fields["from_dataset_uuid"])
    to_dataset = model.datasets.get(id=fields["to_dataset_uuid"])
    join_expression = (
        f"{{{from_dataset.name}.{fields['from_field']}}} = "
        f"{{{to_dataset.name}.{fields['to_field']}}}"
    )
    metadata = {"source": CANVAS_SOURCE, "description": fields.get("description", "")}
    if user is not None and getattr(user, "id", None):
        metadata["created_by"] = str(user.id)
    return SemanticRelationship.objects.create(
        id=change.object_uuid,
        workspace=workspace,
        name=fields["name"],
        from_dataset=from_dataset,
        to_dataset=to_dataset,
        relationship_type=fields["relationship_type"],
        join_expression=join_expression,
        metadata=metadata,
    )


def _create_custom_dataset(canvas, model, workspace, change, user):
    from apps.semantic.canvas.service import custom_dataset_primary_key

    fields = change.fields
    validation = fields.get("_validation") or {}
    compiled_sql = validation.get("compiled_sql", "")
    columns = validation.get("columns", [])
    primary_key = custom_dataset_primary_key(fields, columns)
    custom = CustomDataset.objects.create(
        workspace=workspace,
        name=fields["name"],
        label=fields.get("label", ""),
        description=fields.get("description", ""),
        definition_sql=fields["definition_sql"],
        status=CustomDataset.Status.ACTIVE,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    schema_name = (
        model.datasets.filter(source_kind=SemanticDataset.SourceKind.PHYSICAL, is_visible=True)
        .values_list("schema_name", flat=True)
        .first()
        or ""
    )
    dataset = SemanticDataset.objects.create(
        id=change.object_uuid,
        semantic_model=model,
        workspace=workspace,
        name=fields["name"],
        label=fields.get("label", ""),
        description=fields.get("description", ""),
        source_kind=SemanticDataset.SourceKind.CUSTOM,
        custom_dataset=custom,
        schema_name=schema_name,
        table_name=fields["name"],
        primary_key=primary_key,
        is_visible=True,
        metadata={
            "source_type": "custom",
            "source": CANVAS_SOURCE,
            "cube_sql": compiled_sql,
            "row_count_verified": False,
        },
    )
    _sync_fields(dataset, columns, None)
    return dataset


def _settle_as_membership(change, obj) -> None:
    """Convert a committed create/update row into a membership row."""
    if change.object_type == ObjectType.CUSTOM_DATASET:
        # The draft became a real dataset; the canvas row tracks it as one.
        change.object_type = ObjectType.DATASET
    change.change_type = ChangeType.UPDATE
    change.fields = {}
    change.base_fingerprint = obj.updated_at
    change.save(
        update_fields=["object_type", "change_type", "fields", "base_fingerprint", "updated_at"]
    )
