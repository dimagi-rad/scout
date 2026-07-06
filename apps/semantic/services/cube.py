"""Generate Cube model definitions from Scout semantic-model objects."""

from __future__ import annotations

from typing import Any

import yaml

from apps.semantic.models import SemanticField, SemanticModel, SemanticRelationship


def generate_cube_schema(model: SemanticModel) -> dict[str, Any]:
    """Return a Cube-compatible schema document derived from a semantic model."""
    relationships = SemanticRelationship.objects.filter(workspace=model.workspace).select_related(
        "from_dataset",
        "to_dataset",
    )
    joins_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for relationship in relationships:
        # Cube refuses to compile a cube that defines a join but no primary
        # key; skipping the join keeps the rest of the schema buildable while
        # the relationship stays visible in the catalog.
        if not relationship.from_dataset.primary_key:
            continue
        joins_by_dataset.setdefault(relationship.from_dataset.name, []).append(
            {
                "name": relationship.to_dataset.name,
                "relationship": relationship.relationship_type,
                "sql": relationship.join_expression,
            }
        )

    cubes = []
    for dataset in model.datasets.filter(is_visible=True).prefetch_related("fields"):
        fields = [field for field in dataset.fields.all() if field.is_visible]
        dimensions = [
            _cube_dimension(field, is_primary_key=_is_primary_key_field(dataset, field))
            for field in fields
            if field.field_type
            in {SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION}
        ]
        measures = [
            _cube_measure(field)
            for field in fields
            if field.field_type == SemanticField.FieldType.MEASURE
        ]
        cube: dict[str, Any] = {
            "name": dataset.name,
            "dimensions": dimensions,
            "measures": measures,
        }
        # Cube's YAML compiler coerces '' to null and then rejects it
        # ("description must be a string"), so empty descriptions are omitted.
        if dataset.description:
            cube["description"] = dataset.description
        if dataset.source_kind == dataset.SourceKind.CUSTOM:
            cube_sql = dataset.metadata.get("cube_sql") or dataset.metadata.get("sql")
            if not cube_sql:
                continue
            cube["sql"] = cube_sql
        else:
            # Deliberately unqualified: the physical schema is resolved per query
            # via the search_path that cube.js sets from the security context, so
            # a blue-green tenant-schema swap does not invalidate this YAML.
            cube["sql_table"] = _quote_identifier(dataset.table_name)
        joins = joins_by_dataset.get(dataset.name)
        if joins:
            cube["joins"] = joins
        cubes.append(cube)

    return {
        "model": {
            "id": str(model.id),
            "name": model.name,
            "version": model.version,
        },
        "cubes": cubes,
    }


def generate_cube_schema_yaml(model: SemanticModel) -> str:
    """Return Cube YAML content for the active semantic model."""
    schema = generate_cube_schema(model)
    return yaml.safe_dump(
        {"cubes": schema["cubes"]},
        sort_keys=False,
        allow_unicode=False,
    )


def _is_primary_key_field(dataset, field: SemanticField) -> bool:
    return bool(dataset.primary_key) and field.expression == dataset.primary_key


def _cube_dimension(field: SemanticField, *, is_primary_key: bool = False) -> dict[str, Any]:
    payload = {
        "name": field.name,
        "sql": _cube_sql(field.expression),
        "type": "time" if field.field_type == SemanticField.FieldType.TIME_DIMENSION else _cube_type(field.data_type),
    }
    if is_primary_key:
        payload["primary_key"] = True
        # Cube hides primary-key dimensions by default; keep it queryable.
        payload["public"] = True
    if field.description:
        payload["description"] = field.description
    _apply_display_metadata(payload, field)
    return payload


def _cube_measure(field: SemanticField) -> dict[str, Any]:
    measure_type = field.measure_type or SemanticField.MeasureType.NUMBER
    payload = {
        "name": field.name,
        "type": "number" if measure_type == SemanticField.MeasureType.NUMBER else measure_type,
    }
    metadata = field.metadata or {}
    cube_sql = metadata.get("cube_sql")
    if isinstance(cube_sql, str) and cube_sql.strip():
        payload["sql"] = cube_sql.strip()
    elif measure_type != SemanticField.MeasureType.COUNT:
        payload["sql"] = _cube_sql(field.expression)
    filters = _cube_measure_filters(metadata.get("filters"))
    if filters:
        payload["filters"] = filters
    if field.description:
        payload["description"] = field.description
    _apply_display_metadata(payload, field)
    return payload


def _cube_measure_filters(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    filters: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        sql = item.get("sql")
        if isinstance(sql, str) and sql.strip():
            filters.append({"sql": sql.strip()})
    return filters


def _apply_display_metadata(payload: dict[str, Any], field: SemanticField) -> None:
    metadata = field.metadata or {}
    display_format = metadata.get("format")
    if isinstance(display_format, str) and display_format.strip():
        payload["format"] = display_format.strip()
    currency = metadata.get("currency")
    if isinstance(currency, str) and currency.strip():
        payload["currency"] = currency.strip().upper()


def _cube_type(data_type: str) -> str:
    lowered = data_type.lower()
    if any(token in lowered for token in ("int", "numeric", "decimal", "double", "real")):
        return "number"
    if "bool" in lowered:
        return "boolean"
    return "string"


def _cube_sql(expression: str) -> str:
    if expression == "*":
        return "*"
    return f"{{CUBE}}.{_quote_identifier(expression)}"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
