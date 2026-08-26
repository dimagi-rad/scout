"""Canvas resolution and the single write path (`apply_operations`).

All writes — UI and agent — are batches of ops. A batch is atomic: one invalid
op rejects the whole batch with a structured per-op error. Ops touch only the
paths they name; omitting a field never clears it.

Op vocabulary (see docs/canvas-design.md):

    {"op": "add_existing", "object_type": "dataset", "ref": "raw_visits"}
    {"op": "set", "target": "dataset/raw_visits/description", "value": "..."}
    {"op": "set", "target": "field/<uuid>/label", "value": "..."}
    {"op": "create", "object_type": "field", "value": {"dataset": "raw_visits",
        "name": "total_amount", "field_type": "measure", "measure_type": "sum",
        "expression": "amount"}}
    {"op": "create", "object_type": "relationship", "value": {"from_dataset":
        "raw_visits", "from_field": "username", "to_dataset": "raw_users",
        "to_field": "username", "relationship_type": "many_to_one"}}
    {"op": "create", "object_type": "custom_dataset", "value": {"name":
        "visit_stats", "definition_sql": "select ... from raw_visits ..."}}
    {"op": "delete_object", "object": "field/<uuid>"}
    {"op": "remove_from_canvas", "object": "dataset/raw_visits"}
    {"op": "revert_object", "object": "dataset/raw_visits"}
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid
from typing import Any

from django.db import transaction

from apps.semantic.canvas.objects import (
    CANVAS_SOURCE,
    CUSTOM_DATASET_DRAFT_KEYS,
    DATASET_EDITABLE_KEYS,
    FIELD_CURATION_KEYS,
    FIELD_DRAFT_KEYS,
    FIELD_MEASURE_OPTION_KEYS,
    FIELD_METADATA_KEYS,
    FIELD_TYPES,
    MEASURE_TYPES,
    RELATIONSHIP_DRAFT_KEYS,
    RELATIONSHIP_TYPES,
    ObjectResolutionError,
    base_object_for_change,
    is_canvas_created,
    is_custom_dataset_field,
    is_generated_relationship,
    resolve_dataset,
    resolve_field,
    resolve_relationship,
    serialize_base,
)
from apps.semantic.models import (
    SemanticCanvas,
    SemanticCanvasChange,
    SemanticDataset,
)
from apps.semantic.services.catalog import get_active_semantic_model, semantic_name
from apps.semantic.services.custom_datasets import (
    CustomDatasetError,
    compile_custom_dataset_sql,
    infer_custom_dataset_columns,
)

ObjectType = SemanticCanvasChange.ObjectType
ChangeType = SemanticCanvasChange.ChangeType

SUPPORTED_OPS = {
    "add_existing",
    "set",
    "create",
    "delete_object",
    "remove_from_canvas",
    "revert_object",
}


class CanvasOperationError(ValueError):
    """A structured, per-op failure. The whole batch is rejected."""

    def __init__(self, op_index: int, code: str, message: str) -> None:
        super().__init__(message)
        self.op_index = op_index
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {"op_index": self.op_index, "code": self.code, "message": str(self)}


def resolve_thread_canvas(workspace, thread, user=None) -> SemanticCanvas:
    """Return the thread's canvas, creating it on first touch."""
    model = get_active_semantic_model(workspace)
    canvas, _created = SemanticCanvas.objects.get_or_create(
        thread=thread,
        defaults={
            "workspace": workspace,
            "semantic_model": model,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
        },
    )
    return canvas


def derive_state(change: SemanticCanvasChange, base) -> str:
    """Derive the object state from the change row + its persisted base."""
    if change.change_type == ChangeType.CREATE:
        return "new"
    if base is None:
        return "conflict"
    if change.change_type == ChangeType.DELETE:
        if change.base_fingerprint and base.updated_at != change.base_fingerprint:
            return "conflict"
        return "deleted"
    if not change.fields:
        return "unchanged"
    if change.base_fingerprint and base.updated_at != change.base_fingerprint:
        return "conflict"
    return "edited"


def apply_operations(canvas: SemanticCanvas, operations: list, user=None) -> dict[str, Any]:
    """Apply one atomic op batch; returns applied summaries + diff + diagnostics.

    Raises nothing: invalid batches return ``{"errors": [...]}`` with nothing
    written.
    """
    from apps.semantic.canvas.projections import canvas_projection

    if not isinstance(operations, list) or not operations:
        return {
            "errors": [
                {"op_index": 0, "code": "INVALID_BATCH", "message": "Provide a list of ops."}
            ]
        }

    applied: list[dict[str, Any]] = []
    try:
        with transaction.atomic():
            model = canvas.semantic_model
            for index, raw_op in enumerate(operations):
                applied.append(_apply_one(canvas, model, index, raw_op, user))
    except CanvasOperationError as exc:
        return {"errors": [exc.as_dict()]}

    canvas.save(update_fields=["updated_at"])
    projection = canvas_projection(canvas)
    return {
        "applied": applied,
        "objects": projection["objects"],
        "diagnostics": projection["diagnostics"],
        "can_commit": projection["can_commit"],
    }


def _apply_one(canvas, model, index: int, raw_op, user) -> dict[str, Any]:
    if not isinstance(raw_op, dict):
        raise CanvasOperationError(index, "INVALID_OP", "Each op must be an object.")
    op = raw_op.get("op")
    if op not in SUPPORTED_OPS:
        raise CanvasOperationError(
            index, "UNKNOWN_OP", f"Unknown op '{op}'. Supported: {', '.join(sorted(SUPPORTED_OPS))}."
        )
    handler = {
        "add_existing": _op_add_existing,
        "set": _op_set,
        "create": _op_create,
        "delete_object": _op_delete_object,
        "remove_from_canvas": _op_remove_from_canvas,
        "revert_object": _op_revert_object,
    }[op]
    return handler(canvas, model, index, raw_op, user)


def _op_add_existing(canvas, model, index, raw_op, user) -> dict[str, Any]:
    object_type = raw_op.get("object_type", "dataset")
    if object_type != "dataset":
        raise CanvasOperationError(
            index, "INVALID_OP", "add_existing supports object_type 'dataset' only."
        )
    ref = raw_op.get("ref") or raw_op.get("name") or raw_op.get("uuid") or ""
    dataset = _resolve(index, resolve_dataset, model, str(ref))
    SemanticCanvasChange.objects.get_or_create(
        canvas=canvas,
        object_uuid=dataset.id,
        defaults={
            "object_type": ObjectType.DATASET,
            "change_type": ChangeType.UPDATE,
            "fields": {},
            "base_fingerprint": dataset.updated_at,
        },
    )
    return {"op": "add_existing", "object": f"dataset/{dataset.name}"}


def _op_set(canvas, model, index, raw_op, user) -> dict[str, Any]:
    target = raw_op.get("target") or ""
    parts = str(target).split("/")
    if len(parts) != 3:
        raise CanvasOperationError(
            index, "INVALID_TARGET", "set target must be '<object_type>/<ref>/<key>'."
        )
    object_type, ref, key = parts
    value = raw_op.get("value")
    if object_type == "field":
        key = _normalize_field_key(key)
    accepts_structured_value = object_type == "field" and key in FIELD_MEASURE_OPTION_KEYS
    if value is not None and not isinstance(value, str) and not accepts_structured_value:
        raise CanvasOperationError(index, "INVALID_VALUE", f"'{key}' must be a string.")
    if value is None:
        value = [] if accepts_structured_value and key == "filters" else ""

    draft = _find_draft(canvas, object_type, ref)
    if draft is not None:
        _set_on_draft(index, draft, key, value)
        return {"op": "set", "target": f"{object_type}/{draft.object_uuid}/{key}"}

    if object_type == "dataset":
        dataset = _resolve(index, resolve_dataset, model, ref)
        allowed = DATASET_EDITABLE_KEYS
        if dataset.source_kind == SemanticDataset.SourceKind.CUSTOM:
            # Cube needs a primary key on every joinable cube; CTE datasets
            # have no physical constraint to introspect, so their owner picks.
            allowed = allowed | {"primary_key"}
        _require_key(index, key, allowed, "dataset")
        if key == "primary_key":
            from apps.semantic.canvas.objects import dataset_column_names

            columns = dataset_column_names(dataset)
            if value and value not in columns:
                raise CanvasOperationError(
                    index,
                    "UNKNOWN_COLUMN",
                    f"'{value}' is not a column on {dataset.name}. "
                    f"Columns: {', '.join(sorted(columns)) or '(none)'}.",
                )
        _stage_update(canvas, ObjectType.DATASET, dataset, key, value)
        return {"op": "set", "target": f"dataset/{dataset.name}/{key}"}
    if object_type == "field":
        field = _resolve(index, resolve_field, model, ref)
        allowed = FIELD_DRAFT_KEYS if is_canvas_created(field) else FIELD_CURATION_KEYS
        if key not in allowed:
            raise CanvasOperationError(
                index,
                "PROTECTED_FIELD",
                f"'{key}' is not editable on this field"
                + (
                    " (autogenerated fields accept label, description, format, and currency curation only)."
                    if key in FIELD_DRAFT_KEYS
                    else "."
                ),
            )
        value = _normalize_field_edit(index, key, value)
        _stage_update(canvas, ObjectType.FIELD, field, key, value)
        return {"op": "set", "target": f"field/{field.dataset.name}.{field.name}/{key}"}
    if object_type == "relationship":
        relationship = _resolve(index, resolve_relationship, canvas.workspace, ref)
        if is_generated_relationship(relationship) or not is_canvas_created(relationship):
            raise CanvasOperationError(
                index,
                "PROTECTED_OBJECT",
                "Pipeline-derived relationships cannot be edited. Create a new one instead.",
            )
        _require_key(index, key, RELATIONSHIP_DRAFT_KEYS, "relationship")
        _stage_update(canvas, ObjectType.RELATIONSHIP, relationship, key, value)
        return {"op": "set", "target": f"relationship/{relationship.name}/{key}"}
    raise CanvasOperationError(
        index, "INVALID_TARGET", f"Unknown object type '{object_type}' in set target."
    )


def _op_create(canvas, model, index, raw_op, user) -> dict[str, Any]:
    object_type = raw_op.get("object_type")
    value = raw_op.get("value")
    if not isinstance(value, dict):
        raise CanvasOperationError(index, "INVALID_VALUE", "create requires an object 'value'.")

    if object_type == "field":
        fields = _validated_field_draft(canvas, model, index, value)
        object_uuid = uuid.uuid4()
        change_type_object = ObjectType.FIELD
    elif object_type == "relationship":
        fields = _validated_relationship_draft(model, index, value)
        object_uuid = uuid.uuid4()
        change_type_object = ObjectType.RELATIONSHIP
    elif object_type == "custom_dataset":
        fields = _validated_custom_dataset_draft(canvas, model, index, value)
        object_uuid = uuid.uuid4()
        change_type_object = ObjectType.CUSTOM_DATASET
    else:
        raise CanvasOperationError(
            index,
            "INVALID_OP",
            "create supports object_type 'field', 'relationship', or 'custom_dataset'.",
        )

    SemanticCanvasChange.objects.create(
        canvas=canvas,
        object_type=change_type_object,
        object_uuid=object_uuid,
        change_type=ChangeType.CREATE,
        fields=fields,
    )
    return {
        "op": "create",
        "object": f"{object_type}/{object_uuid}",
        "name": fields.get("name", ""),
    }


def _op_delete_object(canvas, model, index, raw_op, user) -> dict[str, Any]:
    object_type, ref = _parse_object_ref(index, raw_op)
    draft = _find_draft(canvas, object_type, ref)
    if draft is not None:
        draft.delete()
        return {"op": "delete_object", "object": f"{object_type}/{ref}", "dropped_draft": True}

    if object_type == "field":
        field = _resolve(index, resolve_field, model, ref)
        if not (is_canvas_created(field) or is_custom_dataset_field(field)):
            raise CanvasOperationError(
                index,
                "PROTECTED_FIELD",
                "Autogenerated fields on physical datasets cannot be removed. "
                "Only canvas-created fields or fields on custom datasets can be deleted.",
            )
        _stage_delete(canvas, ObjectType.FIELD, field)
        return {"op": "delete_object", "object": f"field/{field.dataset.name}.{field.name}"}
    if object_type == "relationship":
        relationship = _resolve(index, resolve_relationship, canvas.workspace, ref)
        if is_generated_relationship(relationship):
            raise CanvasOperationError(
                index,
                "PROTECTED_OBJECT",
                "Pipeline-derived relationships cannot be removed through the canvas.",
            )
        _stage_delete(canvas, ObjectType.RELATIONSHIP, relationship)
        return {"op": "delete_object", "object": f"relationship/{relationship.name}"}
    if object_type == "dataset":
        dataset = _resolve(index, resolve_dataset, model, ref)
        if dataset.source_kind != SemanticDataset.SourceKind.CUSTOM:
            raise CanvasOperationError(
                index,
                "PROTECTED_OBJECT",
                "Physical datasets cannot be removed. Only CTE (custom) datasets "
                "created through the canvas can be deleted.",
            )
        _stage_delete(canvas, ObjectType.DATASET, dataset)
        return {"op": "delete_object", "object": f"dataset/{dataset.name}"}
    raise CanvasOperationError(index, "INVALID_TARGET", f"Cannot delete '{object_type}'.")


def _op_remove_from_canvas(canvas, model, index, raw_op, user) -> dict[str, Any]:
    object_type, ref = _parse_object_ref(index, raw_op)
    change = _find_change(canvas, model, index, object_type, ref)
    change.delete()
    return {"op": "remove_from_canvas", "object": f"{object_type}/{ref}"}


def _op_revert_object(canvas, model, index, raw_op, user) -> dict[str, Any]:
    object_type, ref = _parse_object_ref(index, raw_op)
    change = _find_change(canvas, model, index, object_type, ref)
    if change.change_type == ChangeType.CREATE:
        change.delete()
        return {"op": "revert_object", "object": f"{object_type}/{ref}", "dropped_draft": True}
    base = base_object_for_change(canvas.semantic_model, canvas.workspace, change)
    change.change_type = ChangeType.UPDATE
    change.fields = {}
    change.base_fingerprint = base.updated_at if base is not None else None
    change.save(update_fields=["change_type", "fields", "base_fingerprint", "updated_at"])
    return {"op": "revert_object", "object": f"{object_type}/{ref}"}


def _parse_object_ref(index: int, raw_op: dict) -> tuple[str, str]:
    target = raw_op.get("object") or raw_op.get("target") or ""
    parts = str(target).split("/")
    if len(parts) != 2 or parts[0] not in {"dataset", "field", "relationship", "custom_dataset"}:
        raise CanvasOperationError(
            index, "INVALID_TARGET", "object must be '<object_type>/<name-or-uuid>'."
        )
    return parts[0], parts[1]


def _resolve(index: int, resolver, scope, ref: str):
    try:
        return resolver(scope, ref)
    except ObjectResolutionError as exc:
        raise CanvasOperationError(index, exc.code, str(exc)) from exc


def _find_draft(canvas, object_type: str, ref: str) -> SemanticCanvasChange | None:
    """Resolve a pending create row by uuid, draft name, or dataset.name."""
    try:
        mapped = ObjectType(object_type)
    except ValueError:
        return None
    drafts = canvas.changes.filter(object_type=mapped, change_type=ChangeType.CREATE)
    ref_uuid = None
    with contextlib.suppress(TypeError, ValueError):
        ref_uuid = uuid.UUID(ref)
    for draft in drafts:
        if ref_uuid and draft.object_uuid == ref_uuid:
            return draft
        name = draft.fields.get("name", "")
        if not ref_uuid and name:
            if ref == name:
                return draft
            dataset_name = draft.fields.get("dataset_name", "")
            if dataset_name and ref == f"{dataset_name}.{name}":
                return draft
    return None


def _find_change(canvas, model, index, object_type: str, ref: str) -> SemanticCanvasChange:
    draft = _find_draft(canvas, object_type, ref)
    if draft is not None:
        return draft
    resolver = {
        "dataset": lambda: resolve_dataset(model, ref),
        "field": lambda: resolve_field(model, ref),
        "relationship": lambda: resolve_relationship(canvas.workspace, ref),
    }.get(object_type)
    if resolver is None:
        raise CanvasOperationError(index, "OBJECT_NOT_FOUND", f"No canvas row for '{ref}'.")
    try:
        obj = resolver()
    except ObjectResolutionError as exc:
        raise CanvasOperationError(index, exc.code, str(exc)) from exc
    change = canvas.changes.filter(object_uuid=obj.id).first()
    if change is None:
        raise CanvasOperationError(
            index, "OBJECT_NOT_FOUND", f"'{object_type}/{ref}' is not on the canvas."
        )
    return change


def _set_on_draft(index: int, draft: SemanticCanvasChange, key: str, value: str) -> None:
    if draft.object_type == ObjectType.FIELD:
        key = _normalize_field_key(key)
    allowed = {
        ObjectType.FIELD: FIELD_DRAFT_KEYS,
        ObjectType.RELATIONSHIP: RELATIONSHIP_DRAFT_KEYS,
        ObjectType.CUSTOM_DATASET: CUSTOM_DATASET_DRAFT_KEYS,
    }[SemanticCanvasChange.ObjectType(draft.object_type)]
    _require_key(index, key, allowed, draft.object_type)
    if draft.object_type == ObjectType.FIELD:
        value = _normalize_field_edit(index, key, value)
    fields = dict(draft.fields)
    if key == "name":
        value = semantic_name(value)
    fields[key] = value
    if draft.object_type == ObjectType.CUSTOM_DATASET and key in {"name", "definition_sql"}:
        # SQL revalidation happens lazily in diagnostics; drop the stale result.
        fields.pop("_validation", None)
    draft.fields = fields
    draft.save(update_fields=["fields", "updated_at"])


def _require_key(index: int, key: str, allowed: frozenset[str], label: str) -> None:
    if key not in allowed:
        raise CanvasOperationError(
            index,
            "INVALID_TARGET",
            f"'{key}' is not an editable {label} key. Editable: {', '.join(sorted(allowed))}.",
        )


def _stage_update(canvas, object_type, obj, key: str, value: str) -> None:
    change, _created = SemanticCanvasChange.objects.get_or_create(
        canvas=canvas,
        object_uuid=obj.id,
        defaults={
            "object_type": object_type,
            "change_type": ChangeType.UPDATE,
            "fields": {},
            "base_fingerprint": obj.updated_at,
        },
    )
    fields = dict(change.fields)
    base_value = _editable_base_value(obj, key)
    if value == base_value:
        fields.pop(key, None)
    else:
        fields[key] = value
    if not change.fields and fields:
        # First pending delta on a membership row: pin the base the editor saw.
        change.base_fingerprint = obj.updated_at
    change.fields = fields
    change.change_type = ChangeType.UPDATE
    change.save(update_fields=["fields", "change_type", "base_fingerprint", "updated_at"])


def _editable_base_value(obj, key: str) -> str:
    if key in FIELD_METADATA_KEYS:
        metadata = getattr(obj, "metadata", None) or {}
        if key == "filters":
            return metadata.get(key) or []
        return str(metadata.get(key) or "")
    return str(getattr(obj, key, "") or "")


def _stage_delete(canvas, object_type, obj) -> None:
    SemanticCanvasChange.objects.update_or_create(
        canvas=canvas,
        object_uuid=obj.id,
        defaults={
            "object_type": object_type,
            "change_type": ChangeType.DELETE,
            "fields": {},
            "base_fingerprint": obj.updated_at,
        },
    )


def _validated_field_draft(canvas, model, index: int, value: dict) -> dict[str, Any]:
    value = _normalized_field_draft_value(index, value)
    unknown = set(value) - FIELD_DRAFT_KEYS - {"dataset"}
    if unknown:
        raise CanvasOperationError(
            index, "INVALID_VALUE", f"Unknown field keys: {', '.join(sorted(unknown))}."
        )
    dataset_ref = str(value.get("dataset") or "")
    dataset = _resolve(index, resolve_dataset, model, dataset_ref)
    name = semantic_name(str(value.get("name") or ""))
    if not name or name == "field":
        raise CanvasOperationError(index, "INVALID_NAME", "A field needs a valid name.")
    field_type = str(value.get("field_type") or "")
    if field_type not in FIELD_TYPES:
        raise CanvasOperationError(
            index,
            "INVALID_FIELD_TYPE",
            f"field_type must be one of: {', '.join(sorted(FIELD_TYPES))}.",
        )
    measure_type = str(value.get("measure_type") or "")
    if field_type == "measure" and measure_type not in MEASURE_TYPES:
        raise CanvasOperationError(
            index,
            "INVALID_MEASURE_TYPE",
            f"A measure needs measure_type: {', '.join(sorted(MEASURE_TYPES))}.",
        )
    if field_type != "measure" and measure_type:
        raise CanvasOperationError(
            index, "INVALID_MEASURE_TYPE", "measure_type only applies to measures."
        )
    if field_type != "measure":
        _reject_measure_options_on_non_measure(index, value)
    return {
        "dataset_uuid": str(dataset.id),
        "dataset_name": dataset.name,
        "name": name,
        "label": str(value.get("label") or ""),
        "description": str(value.get("description") or ""),
        "field_type": field_type,
        "measure_type": measure_type,
        "expression": str(value.get("expression") or ""),
        "data_type": str(value.get("data_type") or ""),
        "format": _normalize_field_edit(index, "format", str(value.get("format") or "")),
        "currency": _normalize_field_edit(index, "currency", str(value.get("currency") or "")),
        "filters": _normalize_field_edit(index, "filters", value.get("filters") or []),
        "cube_sql": _normalize_field_edit(index, "cube_sql", str(value.get("cube_sql") or "")),
    }


_NAMED_FORMAT_RE = re.compile(r"^(number|percent|currency|abbr|accounting)(?:_(\d+))?$")
_DECIMAL_ALIAS_RE = re.compile(r"^decimal_(\d+)$")


def _normalize_field_edit(index: int, key: str, value: str) -> str:
    key = _normalize_field_key(key)
    if key == "format":
        return _normalize_display_format(index, value)
    if key == "currency":
        return _normalize_currency(index, value)
    if key == "filters":
        return _normalize_measure_filters(index, value)
    if key == "cube_sql":
        return _normalize_cube_sql(index, value)
    return value


def _normalize_field_key(key: str) -> str:
    lowered = str(key or "").strip().lower()
    if lowered in {"sql", "measure_sql"}:
        return "cube_sql"
    if lowered in {"filter", "measure_filter", "measure_filters"}:
        return "filters"
    return lowered or str(key or "")


def _normalized_field_draft_value(index: int, value: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    aliases = {
        "sql": "cube_sql",
        "measure_sql": "cube_sql",
        "filter": "filters",
        "measure_filter": "filters",
        "measure_filters": "filters",
    }
    for raw_key, raw_value in value.items():
        key = aliases.get(str(raw_key), str(raw_key))
        if key in normalized and normalized[key] != raw_value:
            raise CanvasOperationError(
                index,
                "INVALID_VALUE",
                f"Field option '{raw_key}' conflicts with another value for '{key}'.",
            )
        normalized[key] = raw_value
    return normalized


def _reject_measure_options_on_non_measure(index: int, value: dict[str, Any]) -> None:
    used = [key for key in FIELD_MEASURE_OPTION_KEYS if value.get(key)]
    if used:
        raise CanvasOperationError(
            index,
            "INVALID_FIELD_OPTION",
            f"{', '.join(sorted(used))} only applies to measures.",
        )


def _normalize_measure_filters(index: int, value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    raw_filters = value if isinstance(value, list) else [value]
    normalized: list[dict[str, str]] = []
    for filter_index, item in enumerate(raw_filters):
        if isinstance(item, str):
            sql = item.strip()
            extra = {}
        elif isinstance(item, dict):
            extra = {str(key): val for key, val in item.items() if key != "sql"}
            sql = str(item.get("sql") or "").strip()
        else:
            raise CanvasOperationError(
                index,
                "INVALID_MEASURE_FILTER",
                f"filters[{filter_index}] must be a SQL string or an object with sql.",
            )
        if extra:
            raise CanvasOperationError(
                index,
                "INVALID_MEASURE_FILTER",
                f"filters[{filter_index}] only supports the Cube 'sql' key.",
            )
        if not sql:
            raise CanvasOperationError(
                index, "INVALID_MEASURE_FILTER", f"filters[{filter_index}] needs sql."
            )
        if len(sql) > 1000:
            raise CanvasOperationError(
                index,
                "INVALID_MEASURE_FILTER",
                f"filters[{filter_index}].sql must be 1000 characters or fewer.",
            )
        normalized.append({"sql": sql})
    return normalized


def _normalize_cube_sql(index: int, value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) > 1000:
        raise CanvasOperationError(
            index, "INVALID_CUBE_SQL", "cube_sql must be 1000 characters or fewer."
        )
    return value


def _normalize_display_format(index: int, value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    decimal_alias = _DECIMAL_ALIAS_RE.fullmatch(value.lower())
    if decimal_alias:
        return f"number_{int(decimal_alias.group(1))}"
    named = _NAMED_FORMAT_RE.fullmatch(value.lower())
    if named:
        digits = named.group(2)
        return named.group(1) if digits is None else f"{named.group(1)}_{int(digits)}"
    if len(value) > 64 or any(char.isspace() for char in value):
        raise CanvasOperationError(
            index,
            "INVALID_FORMAT",
            "format must be a Cube format like number_2, currency_2, percent_1, or a compact d3 spec.",
        )
    return value


def _normalize_currency(index: int, value: str) -> str:
    value = value.strip().upper()
    if not value:
        return ""
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise CanvasOperationError(index, "INVALID_CURRENCY", "currency must be a 3-letter ISO code.")
    return value


def _validated_relationship_draft(model, index: int, value: dict) -> dict[str, Any]:
    unknown = set(value) - RELATIONSHIP_DRAFT_KEYS
    if unknown:
        raise CanvasOperationError(
            index, "INVALID_VALUE", f"Unknown relationship keys: {', '.join(sorted(unknown))}."
        )
    from_dataset = _resolve(index, resolve_dataset, model, str(value.get("from_dataset") or ""))
    to_dataset = _resolve(index, resolve_dataset, model, str(value.get("to_dataset") or ""))
    relationship_type = str(value.get("relationship_type") or "many_to_one")
    if relationship_type not in RELATIONSHIP_TYPES:
        raise CanvasOperationError(
            index,
            "INVALID_RELATIONSHIP_TYPE",
            f"relationship_type must be one of: {', '.join(sorted(RELATIONSHIP_TYPES))}.",
        )
    name = semantic_name(
        str(
            value.get("name")
            or f"{from_dataset.name}_{value.get('from_field', '')}_to_{to_dataset.name}"
        )
    )
    return {
        "name": name,
        "from_dataset_uuid": str(from_dataset.id),
        "from_dataset": from_dataset.name,
        "from_field": semantic_name(str(value.get("from_field") or "")),
        "to_dataset_uuid": str(to_dataset.id),
        "to_dataset": to_dataset.name,
        "to_field": semantic_name(str(value.get("to_field") or "")),
        "relationship_type": relationship_type,
        "description": str(value.get("description") or ""),
    }


def _validated_custom_dataset_draft(canvas, model, index: int, value: dict) -> dict[str, Any]:
    unknown = set(value) - CUSTOM_DATASET_DRAFT_KEYS
    if unknown:
        raise CanvasOperationError(
            index, "INVALID_VALUE", f"Unknown custom dataset keys: {', '.join(sorted(unknown))}."
        )
    name = semantic_name(str(value.get("name") or ""), fallback="dataset")
    if not name or name == "dataset":
        raise CanvasOperationError(index, "INVALID_NAME", "A custom dataset needs a valid name.")
    definition_sql = str(value.get("definition_sql") or "")
    if not definition_sql.strip():
        raise CanvasOperationError(
            index, "INVALID_SQL", "A custom dataset needs definition_sql (a SELECT/CTE query)."
        )
    return {
        "name": name,
        "label": str(value.get("label") or ""),
        "description": str(value.get("description") or ""),
        "definition_sql": definition_sql,
        "primary_key": str(value.get("primary_key") or "").strip(),
    }


def custom_dataset_primary_key(fields: dict[str, Any], columns: list[dict[str, Any]]) -> str:
    """Effective primary key for a CTE draft: the explicit choice, else an
    ``id`` output column. Cube requires one on every joinable cube, so drafts
    that resolve to neither are blocked by MISSING_PRIMARY_KEY diagnostics."""
    explicit = str(fields.get("primary_key") or "").strip()
    if explicit:
        return explicit
    if any(column.get("name") == "id" for column in columns):
        return "id"
    return ""


def allowed_custom_dataset_tables(model) -> dict[str, str]:
    """Physical tables a custom dataset may reference, keyed like the catalog."""
    allowed: dict[str, str] = {}
    for dataset in model.datasets.filter(
        source_kind=SemanticDataset.SourceKind.PHYSICAL, is_visible=True
    ):
        allowed[dataset.name.lower()] = dataset.table_name
        allowed[dataset.table_name.lower()] = dataset.table_name
    return allowed


def validate_custom_dataset_draft(canvas, change: SemanticCanvasChange) -> dict[str, Any]:
    """Compile + probe a custom-dataset draft; cache the result on the row.

    Column inference runs a LIMIT 0 probe against the workspace DB, so the
    result is cached on the change row keyed by the SQL's hash and only
    recomputed when the definition changes.
    """
    fields = dict(change.fields)
    definition_sql = fields.get("definition_sql", "")
    sql_hash = hashlib.sha256(definition_sql.encode("utf-8")).hexdigest()[:16]
    cached = fields.get("_validation") or {}
    if cached.get("sql_hash") == sql_hash:
        return cached

    result: dict[str, Any] = {"sql_hash": sql_hash, "error": "", "columns": [], "compiled_sql": ""}
    try:
        compiled = compile_custom_dataset_sql(
            definition_sql,
            allowed_tables=allowed_custom_dataset_tables(canvas.semantic_model),
        )
        columns = infer_custom_dataset_columns(canvas.workspace, compiled)
        result["compiled_sql"] = compiled
        result["columns"] = columns
    except CustomDatasetError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # workspace context/schema unavailable, etc.
        result["error"] = f"Could not validate SQL: {exc}"

    fields["_validation"] = result
    change.fields = fields
    change.save(update_fields=["fields", "updated_at"])
    return result


def field_expression_columns(dataset: SemanticDataset) -> set[str]:
    from apps.semantic.canvas.objects import dataset_column_names

    return dataset_column_names(dataset)


def touched_field_names(canvas, dataset_uuid: str) -> set[str]:
    """Names of pending created fields for a dataset (for duplicate checks)."""
    names: set[str] = set()
    for change in canvas.changes.filter(
        object_type=ObjectType.FIELD, change_type=ChangeType.CREATE
    ):
        if change.fields.get("dataset_uuid") == dataset_uuid:
            names.add(change.fields.get("name", ""))
    return names


def canvas_created_field_metadata(user) -> dict[str, Any]:
    metadata: dict[str, Any] = {"source": CANVAS_SOURCE}
    if user is not None and getattr(user, "id", None):
        metadata["created_by"] = str(user.id)
    return metadata


def base_and_state(canvas, change: SemanticCanvasChange):
    base = base_object_for_change(canvas.semantic_model, canvas.workspace, change)
    return base, derive_state(change, base), serialize_base(base) if base is not None else {}
