"""Live validation over the canvas changeset.

Diagnostics are computed on read (and after every apply) against the merged
canvas state. Every diagnostic here is introduced by the changeset itself —
persisted-model defects are not the canvas's problem — so all error-severity
diagnostics gate commit.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from apps.semantic.canvas.objects import (
    FIELD_CURATION_KEYS,
    FIELD_TYPES,
    MEASURE_TYPES,
    field_sql_text,
    normalize_member_references,
    references_dataset,
    references_field,
    serialize_field_base,
)
from apps.semantic.canvas.service import (
    ChangeType,
    ObjectType,
    base_and_state,
    custom_dataset_primary_key,
    validate_custom_dataset_draft,
)
from apps.semantic.models import (
    CustomDataset,
    SemanticCanvasChange,
    SemanticDataset,
    SemanticField,
    SemanticRelationship,
)
from apps.semantic.services.cube import cube_dimension_type
from apps.semantic.services.field_sql import (
    DimensionSQLValidationError,
    MeasureSQLValidationError,
    compile_dimension_sql,
    compile_measure_filter_sql,
    compile_measure_sql,
    dataset_column_names,
)

# Cube accepts only these named formats on a non-number dimension (string, time, boolean);
# any other format (number_1, a d3 spec) fails validation of the whole schema (#882).
_STRING_DIMENSION_FORMATS = frozenset(
    {"imageUrl", "link", "currency", "percent", "number", "id", "object"}
)

_DIRECT_MEMBER_DIVISION_RE = re.compile(
    r"\{[A-Za-z_][A-Za-z0-9_.]*\}\s*/\s*"
    r"(?:NULLIF\s*\(\s*)?\{[A-Za-z_][A-Za-z0-9_.]*\}",
    re.IGNORECASE,
)
_DECIMAL_COERCION_RE = re.compile(
    r"::\s*(?:numeric|decimal|double\s+precision|real)\b|"
    r"\bcast\s*\([^)]*\bas\s+(?:numeric|decimal|double\s+precision|real)\b|"
    r"\b\d+\.\d+\s*\*",
    re.IGNORECASE,
)


def compute_diagnostics(
    canvas,
    changes: list[SemanticCanvasChange] | None = None,
    *,
    retry_failed_sql: bool = False,
) -> list[dict]:
    if changes is None:
        changes = list(canvas.changes.all())
    diagnostics: list[dict[str, Any]] = []
    model = canvas.semantic_model

    field_drafts = [
        c
        for c in changes
        if c.object_type == ObjectType.FIELD and c.change_type == ChangeType.CREATE
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
    for change in changes:
        if change.object_type != ObjectType.FIELD or change.change_type == ChangeType.DELETE:
            continue
        base, _state, serialized = base_and_state(canvas, change)
        if change.change_type != ChangeType.CREATE and base is None:
            continue
        fields = {**serialized, **change.fields}
        # Format is a curation key, so a format-only edit skips the expression checks below.
        # A settled row (no fields) is persisted state, which is not this changeset's to block.
        if change.fields:
            diagnostics.extend(_dimension_format_diagnostics(change, fields))
        if change.change_type != ChangeType.CREATE and set(change.fields) - FIELD_CURATION_KEYS:
            diagnostics.extend(_field_expression_diagnostics(base.dataset, change, fields))
        diagnostics.extend(_calculated_measure_diagnostics(change, fields))
    for change in relationship_drafts:
        diagnostics.extend(
            _relationship_draft_diagnostics(
                canvas, model, change, relationship_drafts, field_drafts
            )
        )
    for change in custom_drafts:
        diagnostics.extend(
            _custom_draft_diagnostics(
                canvas, model, change, custom_drafts, retry_failed_sql=retry_failed_sql
            )
        )

    diagnostics.extend(_reference_diagnostics(canvas, model, changes))

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


class _Reference(NamedTuple):
    """SQL that may name members, and who owns it."""

    object_id: str
    dataset_id: str  # "" for a join, which never resolves a bare {name}
    label: str
    text: str


class _Target(NamedTuple):
    change: SemanticCanvasChange
    path: str
    verb: str
    dataset: SemanticDataset
    field: SemanticField | None  # None when the whole dataset goes


def _reference_diagnostics(canvas, model, changes) -> list[dict]:
    """Refuse removing or renaming a member that published SQL or a join still names.

    The Cube build runs after the commit, so a dangling ``{member}`` would
    leave the saved model unbuildable rather than block the save.
    """
    targets: list[_Target] = []
    for change in changes:
        if change.change_type == ChangeType.CREATE:
            continue
        is_delete = change.change_type == ChangeType.DELETE
        if change.object_type == ObjectType.DATASET and is_delete:
            dataset = model.datasets.filter(id=change.object_uuid).first()
            if dataset is not None:
                targets.append(_Target(change, "", "Deleting", dataset, None))
        elif change.object_type == ObjectType.FIELD and (is_delete or "name" in change.fields):
            field = (
                SemanticField.objects.filter(id=change.object_uuid)
                .select_related("dataset")
                .first()
            )
            if field is not None:
                path, verb = ("", "Deleting") if is_delete else ("name", "Renaming")
                targets.append(_Target(change, path, verb, field.dataset, field))
    if not targets:
        return []

    references = _published_references(model, changes, targets)
    out = []
    for target in targets:
        if target.field is None:
            user = next(
                (
                    ref.label
                    for ref in references
                    if references_dataset(ref.text, target.dataset.name)
                ),
                "",
            )
            what = f"dataset {target.dataset.name}"
        else:
            user = next(
                (
                    ref.label
                    for ref in references
                    if ref.object_id != str(target.field.id)
                    and references_field(
                        ref.text,
                        target.dataset.name,
                        target.field.name,
                        same_dataset=ref.dataset_id == str(target.dataset.id),
                    )
                ),
                "",
            )
            what = f"field {target.dataset.name}.{target.field.name}"
        if user:
            out.append(
                _diagnostic(
                    "MEMBER_IN_USE",
                    target.change,
                    target.path,
                    f"{target.verb} {what} would break {user}, which references it. "
                    "Update or remove that reference in the same batch first.",
                )
            )
    return out


def _published_references(model, changes, targets: list[_Target]) -> list[_Reference]:
    """SQL the Cube build will publish once this canvas commits.

    Mirrors ``generate_cube_schema``: hidden datasets and fields, and joins with a
    hidden endpoint, are never published, so what they name cannot break it.
    """
    gone_datasets = {str(t.dataset.id) for t in targets if t.field is None}
    gone_fields = {
        str(t.field.id)
        for t in targets
        if t.field is not None and t.change.change_type == ChangeType.DELETE
    }
    names, hidden = {}, set()
    for id_, name, is_visible in SemanticDataset.objects.filter(semantic_model=model).values_list(
        "id", "name", "is_visible"
    ):
        names[str(id_)] = name
        if not is_visible:
            hidden.add(str(id_))
    unpublished = hidden | gone_datasets
    visible = {id_: name for id_, name in names.items() if id_ not in unpublished}
    pending = {str(change.object_uuid): change for change in changes}
    references = []
    for field in SemanticField.objects.filter(dataset__semantic_model=model, is_visible=True):
        dataset_id = str(field.dataset_id)
        if str(field.id) in gone_fields or dataset_id not in visible:
            continue
        edit = pending.get(str(field.id))
        draft = edit.fields if edit is not None and edit.change_type == ChangeType.UPDATE else {}
        merged = {"expression": field.expression, **(field.metadata or {}), **draft}
        # The persisted name is the one canvas ops resolve.
        label = f"Field {visible[dataset_id]}.{field.name}"
        references.append(_Reference(str(field.id), dataset_id, label, field_sql_text(merged)))
    for change in changes:
        if change.change_type != ChangeType.CREATE:
            continue
        fields = change.fields
        if change.object_type == ObjectType.FIELD:
            # A draft on a dataset this batch deletes still counts: it could not commit.
            dataset_id = str(fields.get("dataset_uuid", ""))
            if dataset_id in hidden:
                continue
            label = f"Field {names.get(dataset_id, '')}.{fields.get('name', '')}"
            references.append(
                _Reference(str(change.object_uuid), dataset_id, label, field_sql_text(fields))
            )
        elif (
            change.object_type == ObjectType.RELATIONSHIP
            and not {
                str(fields.get("from_dataset_uuid", "")),
                str(fields.get("to_dataset_uuid", "")),
            }
            & hidden
        ):
            # Commit synthesizes the join from these names (commit._create_relationship).
            text = (
                f"{{{fields.get('from_dataset', '')}.{fields.get('from_field', '')}}} = "
                f"{{{fields.get('to_dataset', '')}.{fields.get('to_field', '')}}}"
            )
            references.append(
                _Reference(
                    str(change.object_uuid), "", f"Relationship {fields.get('name', '')}", text
                )
            )
    deleted_joins = {
        str(change.object_uuid)
        for change in changes
        if change.object_type == ObjectType.RELATIONSHIP and change.change_type == ChangeType.DELETE
    }
    for relationship in SemanticRelationship.objects.filter(workspace_id=model.workspace_id):
        endpoints = {str(relationship.from_dataset_id), str(relationship.to_dataset_id)}
        if str(relationship.id) in deleted_joins or not endpoints <= visible.keys():
            continue
        references.append(
            _Reference(
                str(relationship.id),
                "",
                f"Relationship {relationship.name}",
                normalize_member_references(relationship.join_expression or ""),
            )
        )
    return references


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

    out.extend(_field_expression_diagnostics(dataset, change, fields))
    return out


def saved_field_diagnostics(field: SemanticField) -> list[dict]:
    """The canvas field contract applied to a saved field, such as one an undo writes back."""
    change = SemanticCanvasChange(
        object_type=ObjectType.FIELD, object_uuid=field.id, fields={"name": field.name}
    )
    values = serialize_field_base(field)
    return [
        *_field_expression_diagnostics(field.dataset, change, values),
        *_dimension_format_diagnostics(change, values),
        *_calculated_measure_diagnostics(change, values),
    ]


def _field_expression_diagnostics(dataset, change, fields: dict[str, Any]) -> list[dict]:
    """Apply the same field contract to creates and the merged state of edits."""
    out: list[dict[str, Any]] = []
    field_type = fields.get("field_type", "")
    measure_type = fields.get("measure_type", "")
    if field_type not in FIELD_TYPES:
        return [
            _diagnostic(
                "INVALID_FIELD_TYPE",
                change,
                "field_type",
                "Choose dimension, time_dimension, or measure.",
            )
        ]
    if field_type == "measure":
        if measure_type not in MEASURE_TYPES:
            out.append(
                _diagnostic(
                    "INVALID_MEASURE_TYPE",
                    change,
                    "measure_type",
                    "A measure needs a supported measure_type.",
                )
            )
    else:
        if measure_type:
            out.append(
                _diagnostic(
                    "INVALID_MEASURE_TYPE",
                    change,
                    "measure_type",
                    "measure_type only applies to measures.",
                )
            )
        if fields.get("filters"):
            out.append(
                _diagnostic(
                    "INVALID_FIELD_OPTION", change, "filters", "filters only applies to measures."
                )
            )
    cube_sql = fields.get("cube_sql")
    columns = dataset_column_names(dataset)
    if field_type == "measure":
        out.extend(_measure_filter_diagnostics(change, fields.get("filters"), columns))
    # Generation ignores non-string measure SQL and publishes the expression instead,
    # so such a value counts as absent and the expression checks below still apply.
    if field_type == "measure" and not (isinstance(cube_sql, str) and cube_sql.strip()):
        cube_sql = None
    if cube_sql:
        if field_type == "measure":
            try:
                compile_measure_sql(cube_sql, columns=columns)
            except MeasureSQLValidationError as exc:
                out.append(_diagnostic("INVALID_MEASURE_SQL", change, "cube_sql", str(exc)))
        else:
            try:
                compile_dimension_sql(cube_sql, columns=columns)
            except DimensionSQLValidationError as exc:
                out.append(_diagnostic("INVALID_DIMENSION_SQL", change, "cube_sql", str(exc)))
        return out
    if field_type == "measure" and measure_type == "count":
        return out
    expression = fields.get("expression", "")
    if not expression:
        out.append(
            _diagnostic(
                "MISSING_EXPRESSION",
                change,
                "expression",
                "Set expression to one of the dataset's columns, or use sql/cube_sql for a calculation"
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
                "name an existing column. Use sql/cube_sql for a row-level calculated "
                "dimension or a calculated measure (measure_type 'number'). Use a "
                "CTE dataset when the calculation changes row grain.",
            )
        )
    return out


def _dimension_format_diagnostics(change, fields: dict[str, Any]) -> list[dict]:
    field_type = fields.get("field_type", "")
    if field_type not in {"dimension", "time_dimension"}:
        return []
    display_format = str(fields.get("format") or "").strip()
    if not display_format or display_format in _STRING_DIMENSION_FORMATS:
        return []
    data_type = str(fields.get("data_type") or "")
    if field_type == "dimension" and cube_dimension_type(data_type) == "number":
        return []
    allowed = ", ".join(sorted(_STRING_DIMENSION_FORMATS))
    if field_type == "time_dimension":
        message = f"A time dimension only takes format {allowed}; '{display_format}' is not one."
    else:
        message = (
            f"format '{display_format}' needs a numeric dimension, but data_type "
            f"'{data_type or '(empty)'}' publishes as a {cube_dimension_type(data_type)}. "
            f"Set data_type to number, or use format {allowed}."
        )
    return [_diagnostic("INVALID_FORMAT", change, "format", message)]


def _measure_filter_diagnostics(change, filters: Any, columns: set[str]) -> list[dict]:
    out: list[dict[str, Any]] = []
    for index, item in enumerate(filters if isinstance(filters, list) else []):
        # Same shape rule as generation, which skips anything else.
        sql = item.get("sql") if isinstance(item, dict) else None
        if not isinstance(sql, str) or not sql.strip():
            continue
        try:
            compile_measure_filter_sql(sql, columns=columns)
        except MeasureSQLValidationError as exc:
            out.append(
                _diagnostic("INVALID_MEASURE_FILTER", change, f"filters[{index}].sql", str(exc))
            )
    return out


def _calculated_measure_diagnostics(change, fields: dict[str, Any]) -> list[dict]:
    """Reject the common count/count ratio that PostgreSQL truncates to zero."""
    if fields.get("measure_type") != SemanticField.MeasureType.NUMBER:
        return []
    cube_sql = str(fields.get("cube_sql") or "")
    if not _DIRECT_MEMBER_DIVISION_RE.search(cube_sql):
        return []
    if _DECIMAL_COERCION_RE.search(cube_sql):
        return []
    return [
        _diagnostic(
            "INTEGER_DIVISION_RISK",
            change,
            "cube_sql",
            "A ratio of count measures needs decimal division in PostgreSQL. "
            "Cast one operand, for example: "
            "{approved_count}::numeric / NULLIF({count}, 0).",
        )
    ]


def _relationship_draft_diagnostics(canvas, model, change, siblings, field_drafts) -> list[dict]:
    out: list[dict[str, Any]] = []
    fields = change.fields
    from_dataset = model.datasets.filter(id=fields.get("from_dataset_uuid")).first()
    to_dataset = model.datasets.filter(id=fields.get("to_dataset_uuid")).first()
    if from_dataset is None or to_dataset is None:
        out.append(_diagnostic("UNKNOWN_DATASET", change, "", "A linked dataset no longer exists."))
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


def _custom_draft_diagnostics(
    canvas, model, change, siblings, *, retry_failed_sql: bool = False
) -> list[dict]:
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

    validation = validate_custom_dataset_draft(canvas, change, retry_failed=retry_failed_sql)
    if validation.get("error"):
        code = validation.get("error_code", "INVALID_SQL")
        path = "" if code == "CATALOG_UNAVAILABLE" else "definition_sql"
        out.append(_diagnostic(code, change, path, validation["error"]))
        return out
    columns = validation.get("columns") or []
    if not columns:
        out.append(
            _diagnostic("INVALID_SQL", change, "definition_sql", "The query returns no columns.")
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
