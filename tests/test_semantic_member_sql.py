"""Stored measure, filter and join SQL is validated when the Cube schema is generated."""

import re

import pytest
import yaml

from apps.semantic.models import (
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services.cube import DroppedJoin, cube_schema_yaml, generate_cube_schema
from apps.semantic.services.cube_sql import embed_cube_sql
from apps.semantic.services.field_sql import MeasureSQLValidationError

pytestmark = pytest.mark.django_db

_DENIED_SQL = "max(pg_advisory_lock(1))"


@pytest.fixture
def model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Member SQL")
    for name in ("visits", "users"):
        dataset = SemanticDataset.objects.create(
            workspace=workspace,
            semantic_model=model,
            name=name,
            table_name=f"raw_{name}",
            primary_key="id",
        )
        for column in ("id", "user_id", "amount"):
            SemanticField.objects.create(
                dataset=dataset,
                name=column,
                expression=column,
                field_type="dimension",
                metadata={"source_column": column},
            )
        SemanticField.objects.create(
            dataset=dataset, name="count", field_type="measure", measure_type="count"
        )
    return model


def _visits(model):
    return model.datasets.get(name="visits")


@pytest.mark.parametrize(
    "metadata",
    [
        {"cube_sql": _DENIED_SQL},
        {"filters": [{"sql": "pg_advisory_lock(1) IS NOT NULL"}]},
    ],
)
def test_stored_denied_measure_sql_cannot_be_published(model, metadata):
    SemanticField.objects.create(
        dataset=_visits(model),
        name="escalating",
        field_type="measure",
        measure_type="number",
        metadata=metadata,
    )

    with pytest.raises(MeasureSQLValidationError, match="pg_advisory_lock"):
        generate_cube_schema(model)


def test_stored_denied_join_is_not_published(model):
    SemanticRelationship.objects.create(
        workspace=model.workspace,
        name="visits_users",
        from_dataset=_visits(model),
        to_dataset=model.datasets.get(name="users"),
        join_expression=f"{{visits.user_id}} = {{users.id}} AND {_DENIED_SQL} IS NULL",
    )

    schema = generate_cube_schema(model)

    visits = next(cube for cube in schema["cubes"] if cube["name"] == "visits")
    assert "joins" not in visits
    assert [d["code"] for d in schema["diagnostics"]] == [DroppedJoin.INVALID_SQL]
    assert schema["diagnostics"][0]["relationship"] == "visits_users"


def test_join_on_owning_cube_column_is_published(model):
    SemanticRelationship.objects.create(
        workspace=model.workspace,
        name="visits_users",
        from_dataset=_visits(model),
        to_dataset=model.datasets.get(name="users"),
        join_expression='{CUBE}."user_id" = {users.id}',
    )

    schema = generate_cube_schema(model)

    visits = next(cube for cube in schema["cubes"] if cube["name"] == "visits")
    assert visits["joins"][0]["sql"] == '{CUBE}."user_id" = {users.id}'
    assert schema["diagnostics"] == []


_TEMPLATE_TEXT = "Rows {{ 7 * 6 }} for {CUBE} {% if x %}y{% endif %} ${z} `q`"


def test_free_text_reaches_cube_yaml_as_literal_text(model):
    visits = _visits(model)
    visits.description = _TEMPLATE_TEXT
    visits.save(update_fields=["description"])
    SemanticField.objects.filter(dataset=visits, name="count").update(
        description=_TEMPLATE_TEXT, metadata={"format": "{x}"}
    )
    SemanticField.objects.filter(dataset=visits, name="amount").update(description=_TEMPLATE_TEXT)

    content = cube_schema_yaml(generate_cube_schema(model))

    # Cube renders a model file as Jinja only when it sees template delimiters.
    assert not re.search(r"\{%|%\}|\{\{|\}\}", content)
    visits_cube = next(c for c in yaml.safe_load(content)["cubes"] if c["name"] == "visits")
    escaped = embed_cube_sql(_TEMPLATE_TEXT)
    assert not any(char in escaped for char in "{}$`")
    assert visits_cube["description"] == escaped
    count = next(m for m in visits_cube["measures"] if m["name"] == "count")
    assert count["description"] == escaped
    assert count["format"] == embed_cube_sql("{x}")
    amount = next(d for d in visits_cube["dimensions"] if d["name"] == "amount")
    assert amount["description"] == escaped
    assert amount["sql"] == '{CUBE}."amount"'
