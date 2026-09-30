"""Stored measure, filter and join SQL is validated when the Cube schema is generated."""

import pytest

from apps.semantic.models import (
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services.cube import DroppedJoin, generate_cube_schema
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
