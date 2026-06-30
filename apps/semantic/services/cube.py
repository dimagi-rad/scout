"""Generate Cube model definitions from Scout semantic-model objects."""

from __future__ import annotations

from typing import Any

from apps.semantic.models import SemanticField, SemanticModel, SemanticRelationship


def generate_cube_schema(model: SemanticModel) -> dict[str, Any]:
    """Return a Cube-compatible schema document derived from a semantic model."""
    relationships = SemanticRelationship.objects.filter(workspace=model.workspace).select_related(
        "from_dataset",
        "to_dataset",
    )
    joins_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for relationship in relationships:
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
            _cube_dimension(field)
            for field in fields
            if field.field_type
            in {SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION}
        ]
        measures = [
            _cube_measure(field)
            for field in fields
            if field.field_type == SemanticField.FieldType.MEASURE
        ]
        cube = {
            "name": dataset.name,
            "sql_table": _sql_table(dataset.schema_name, dataset.table_name),
            "description": dataset.description,
            "dimensions": dimensions,
            "measures": measures,
        }
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


def _cube_dimension(field: SemanticField) -> dict[str, Any]:
    payload = {
        "name": field.name,
        "sql": _cube_sql(field.expression),
        "type": "time" if field.field_type == SemanticField.FieldType.TIME_DIMENSION else _cube_type(field.data_type),
    }
    if field.description:
        payload["description"] = field.description
    return payload


def _cube_measure(field: SemanticField) -> dict[str, Any]:
    measure_type = field.measure_type or SemanticField.MeasureType.NUMBER
    payload = {
        "name": field.name,
        "type": "number" if measure_type == SemanticField.MeasureType.NUMBER else measure_type,
    }
    if measure_type != SemanticField.MeasureType.COUNT:
        payload["sql"] = _cube_sql(field.expression)
    if field.description:
        payload["description"] = field.description
    return payload


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


def _sql_table(schema_name: str, table_name: str) -> str:
    if schema_name:
        return f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
    return _quote_identifier(table_name)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
