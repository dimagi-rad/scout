"""Generate Cube model definitions from Scout semantic-model objects."""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any

import yaml

from apps.semantic.models import SemanticField, SemanticModel, SemanticRelationship
from apps.semantic.services.cube_sql import CubeSQLReferenceError, embed_cube_sql
from apps.semantic.services.field_sql import (
    JoinSQLValidationError,
    compile_dimension_sql,
    compile_join_sql,
    compile_measure_filter_sql,
    compile_measure_sql,
    dataset_column_names,
)

logger = logging.getLogger(__name__)


class DroppedJoin(StrEnum):
    """Why generate_cube_schema left a relationship out of Cube.

    Diagnostics with these codes carry a "relationship" key that the catalog
    turns into published: false.
    """

    UNPUBLISHED_ENDPOINT = "relationship_unpublished_endpoint"
    MISSING_PRIMARY_KEY = "relationship_missing_primary_key"
    HIDDEN_REFERENCE = "relationship_hidden_reference"
    STALE_REFERENCE = "relationship_stale_reference"
    INVALID_SQL = "relationship_invalid_sql"


DROPPED_JOIN_CODES = frozenset(DroppedJoin)
_JINJA_DELIMITERS = re.compile(r"\{%|%\}|\{\{|\}\}|\{#|#\}")


def generate_cube_schema(model: SemanticModel) -> dict[str, Any]:
    """Return a Cube-compatible schema document derived from a semantic model."""
    all_datasets = list(model.datasets.prefetch_related("fields"))
    datasets = publishable_datasets(all_datasets)
    visible_ids = {dataset.id for dataset in datasets}
    datasets_by_id = {dataset.id: dataset for dataset in all_datasets}
    known_references = {dataset.name for dataset in all_datasets} | {
        f"{dataset.name}.{field.name}" for dataset in all_datasets for field in dataset.fields.all()
    }
    references = published_member_references(datasets)
    relationships = SemanticRelationship.objects.filter(workspace=model.workspace).select_related(
        "from_dataset",
        "to_dataset",
    )
    joins_by_dataset: dict[str, list[dict[str, Any]]] = {}
    diagnostics: list[dict[str, Any]] = []
    join_references = references | {"CUBE"}

    def unpublished(relationship: SemanticRelationship, code: DroppedJoin, reason: str) -> None:
        message = f"Relationship '{relationship.name}' was not published: {reason}."
        logger.warning("%s (relationship %s)", message, relationship.id)
        diagnostics.append(
            {
                "level": "warning",
                "code": code.value,
                "relationship": relationship.name,
                "message": message,
            }
        )

    for relationship in relationships:
        endpoints = (relationship.from_dataset, relationship.to_dataset)
        missing = list(
            {dataset.id: dataset for dataset in endpoints if dataset.id not in visible_ids}.values()
        )
        if missing:
            # Catalog refresh hides the dataset of a vanished source table. The
            # catalog lists a relationship under its visible endpoints only, so
            # one with no visible endpoint is advertised nowhere.
            if any(dataset.is_visible for dataset in endpoints):
                unpublished(
                    relationship,
                    DroppedJoin.UNPUBLISHED_ENDPOINT,
                    f"{'datasets' if len(missing) > 1 else 'dataset'} "
                    f"{' and '.join(repr(d.name) for d in missing)} "
                    f"{'are' if len(missing) > 1 else 'is'} hidden, no longer in the source, "
                    "or without SQL",
                )
            continue
        # Cube refuses to compile a cube that defines a join but no primary key.
        if not relationship.from_dataset.primary_key:
            unpublished(
                relationship,
                DroppedJoin.MISSING_PRIMARY_KEY,
                f"dataset '{relationship.from_dataset.name}' has no primary key",
            )
            continue
        try:
            join_sql = embed_cube_sql(
                compile_join_sql(
                    relationship.join_expression,
                    columns=dataset_column_names(datasets_by_id[relationship.from_dataset_id]),
                ),
                references=join_references,
            )
        except JoinSQLValidationError as exc:
            unpublished(
                relationship,
                DroppedJoin.INVALID_SQL,
                f"its join SQL is not publishable: {str(exc)[:300].rstrip('.')}",
            )
            continue
        except CubeSQLReferenceError as exc:
            # A join only adds a path between cubes, so dropping one changes no
            # metric; failing here would take every cube in the workspace down.
            hidden = exc.reference in known_references
            # Catalog refresh hides dropped source columns rather than deleting them.
            state = "hidden or no longer in the source" if hidden else "not in the semantic catalog"
            unpublished(
                relationship,
                DroppedJoin.HIDDEN_REFERENCE if hidden else DroppedJoin.STALE_REFERENCE,
                f"it references '{exc.reference[:200]}', which is {state}",
            )
            continue
        joins_by_dataset.setdefault(relationship.from_dataset.name, []).append(
            {
                "name": relationship.to_dataset.name,
                "relationship": relationship.relationship_type,
                "sql": join_sql,
            }
        )

    cubes = []
    for dataset in datasets:
        fields = [field for field in dataset.fields.all() if field.is_visible]
        columns = dataset_column_names(dataset)
        dimensions = [
            _cube_dimension(
                field, columns=columns, is_primary_key=_is_primary_key_field(dataset, field)
            )
            for field in fields
            if field.field_type
            in {SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION}
        ]
        measure_references = cube_member_references(references, dataset)
        measures = [
            _cube_measure(field, references=measure_references, columns=columns)
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
            source_sql = cube_sql
        else:
            # Deliberately unqualified: the physical schema is resolved per query
            # via the search_path that cube.js sets from the security context, so
            # a blue-green tenant-schema swap does not invalidate this YAML.
            source_sql = f"SELECT * FROM {_quote_identifier(dataset.table_name)}"  # noqa: S608
        cube["sql"] = _publication_scoped_sql(source_sql)
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
        "diagnostics": diagnostics,
    }


def publishable_datasets(all_datasets) -> list:
    """Datasets that become cubes: visible, and custom ones only once they have SQL."""
    return [
        dataset
        for dataset in all_datasets
        if dataset.is_visible
        and (
            dataset.source_kind != dataset.SourceKind.CUSTOM
            or dataset.metadata.get("cube_sql")
            or dataset.metadata.get("sql")
        )
    ]


def published_member_references(datasets) -> set[str]:
    """Cube and member names other SQL may reference as {name} or {cube.member}."""
    return {dataset.name for dataset in datasets} | {
        f"{dataset.name}.{field.name}"
        for dataset in datasets
        for field in dataset.fields.all()
        if field.is_visible
    }


def cube_member_references(references: set[str], dataset) -> set[str]:
    """References a measure or filter of ``dataset`` may use, including its own members."""
    fields = [field for field in dataset.fields.all() if field.is_visible]
    return references | {f.name for f in fields} | {f"CUBE.{f.name}" for f in fields} | {"CUBE"}


def cube_schema_yaml(schema: dict[str, Any]) -> str:
    """Serialize a generated schema's cubes; model info and diagnostics stay out of Cube."""
    content = yaml.safe_dump(
        {"cubes": _literal_text_properties(schema["cubes"])},
        sort_keys=False,
        allow_unicode=False,
    )
    # Cube renders any model file containing these as a Jinja template.
    if _JINJA_DELIMITERS.search(content):
        raise ValueError("Generated Cube schema contains template delimiters.")
    return content


def _literal_text_properties(value: Any, key: str = "") -> Any:
    """Escape every non-SQL string so Cube reads it as literal text.

    Cube renders a YAML model file as a Jinja template when it contains template
    delimiters, then compiles each string property as a Python f-string. Free text
    such as descriptions and formats must survive both passes unchanged; ``sql``
    values were already escaped with their trusted references by the generator.
    """
    if isinstance(value, dict):
        return {k: _literal_text_properties(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_literal_text_properties(item, key) for item in value]
    if isinstance(value, str) and key != "sql":
        return embed_cube_sql(value)
    return value


def _publication_scoped_sql(source_sql: str) -> str:
    # The bound revision fences Cube's SQL result cache AND queue without a
    # driver pool per publication. An empty legacy-context array is also true.
    return (
        f"SELECT * FROM (\n{embed_cube_sql(source_sql)}\n) AS scout_source\n"  # noqa: S608
        "WHERE ARRAY[{SECURITY_CONTEXT.cubeDataRevision}]::text[] IS NOT NULL"
    )


def _is_primary_key_field(dataset, field: SemanticField) -> bool:
    return (
        bool(dataset.primary_key)
        and field.expression == dataset.primary_key
        and not (field.metadata or {}).get("cube_sql")
    )


def _cube_dimension(
    field: SemanticField, *, columns: set[str], is_primary_key: bool = False
) -> dict[str, Any]:
    cube_sql = (field.metadata or {}).get("cube_sql")
    payload = {
        "name": field.name,
        "sql": compile_dimension_sql(cube_sql, columns=columns)
        if cube_sql
        else _cube_sql(field.expression),
        "type": "time"
        if field.field_type == SemanticField.FieldType.TIME_DIMENSION
        else cube_dimension_type(field.data_type),
    }
    if is_primary_key:
        payload["primary_key"] = True
        # Cube hides primary-key dimensions by default; keep it queryable.
        payload["public"] = True
    if field.description:
        payload["description"] = field.description
    _apply_display_metadata(payload, field)
    return payload


def _cube_measure(
    field: SemanticField, *, references: set[str], columns: set[str]
) -> dict[str, Any]:
    measure_type = field.measure_type or SemanticField.MeasureType.NUMBER
    payload = {
        "name": field.name,
        "type": "number" if measure_type == SemanticField.MeasureType.NUMBER else measure_type,
    }
    metadata = field.metadata or {}
    cube_sql = metadata.get("cube_sql")
    if isinstance(cube_sql, str) and cube_sql.strip():
        payload["sql"] = embed_cube_sql(
            compile_measure_sql(cube_sql, columns=columns), references=references
        )
    elif measure_type != SemanticField.MeasureType.COUNT:
        payload["sql"] = _cube_sql(field.expression)
    filters = _cube_measure_filters(metadata.get("filters"), references=references, columns=columns)
    if filters:
        payload["filters"] = filters
    if field.description:
        payload["description"] = field.description
    _apply_display_metadata(payload, field)
    return payload


def _cube_measure_filters(
    value: Any, *, references: set[str], columns: set[str]
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    filters: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        sql = item.get("sql")
        if isinstance(sql, str) and sql.strip():
            filters.append(
                {
                    "sql": embed_cube_sql(
                        compile_measure_filter_sql(sql, columns=columns), references=references
                    )
                }
            )
    return filters


def _apply_display_metadata(payload: dict[str, Any], field: SemanticField) -> None:
    metadata = field.metadata or {}
    display_format = metadata.get("format")
    if isinstance(display_format, str) and display_format.strip():
        payload["format"] = display_format.strip()
    currency = metadata.get("currency")
    if isinstance(currency, str) and currency.strip():
        payload["currency"] = currency.strip().upper()


# A canvas field's data_type is free text, so "number" and "float" must map here too, and
# the catalog formats money as currency_2: as strings, Cube rejects those formats and with
# them the whole schema (#882).
_NUMERIC_TYPE_TOKENS = ("int", "numeric", "decimal", "double", "real", "number", "float", "money")


def cube_dimension_type(data_type: str) -> str:
    lowered = data_type.lower()
    if any(token in lowered for token in _NUMERIC_TYPE_TOKENS):
        return "number"
    if "bool" in lowered:
        return "boolean"
    return "string"


def _cube_sql(expression: str) -> str:
    if expression == "*":
        return "*"
    return f"{{CUBE}}.{embed_cube_sql(_quote_identifier(expression))}"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
