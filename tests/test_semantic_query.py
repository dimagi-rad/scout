import pytest

from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel
from apps.semantic.services import query as query_service
from apps.semantic.services.cube import generate_cube_schema


@pytest.fixture
def semantic_model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Test Semantic Model")
    visits = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="visits",
        label="Visits",
        table_name="raw_visits",
        schema_name="tenant_schema",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="count",
        label="Count",
        field_type=SemanticField.FieldType.MEASURE,
        data_type="integer",
        expression="*",
        measure_type=SemanticField.MeasureType.COUNT,
    )
    SemanticField.objects.create(
        dataset=visits,
        name="username",
        label="Username",
        field_type=SemanticField.FieldType.DIMENSION,
        data_type="text",
        expression="username",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="visit_date",
        label="Visit Date",
        field_type=SemanticField.FieldType.TIME_DIMENSION,
        data_type="timestamp with time zone",
        expression="visit_date",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="sum_amount",
        label="Total Amount",
        field_type=SemanticField.FieldType.MEASURE,
        data_type="numeric",
        expression="amount",
        measure_type=SemanticField.MeasureType.SUM,
    )
    return model


def test_compile_semantic_query_from_members(monkeypatch, workspace, semantic_model):
    monkeypatch.setattr(query_service, "ensure_semantic_model", lambda _workspace: semantic_model)

    compiled = query_service._compile_semantic_query(
        workspace,
        {
            "measures": ["visits.count"],
            "dimensions": ["visits.username"],
            "time_dimension": "visits.visit_date",
            "granularity": "day",
            "filters": [
                {
                    "field": "visits.visit_date",
                    "operator": "inDateRange",
                    "value": ["2026-06-22", "2026-06-28"],
                }
            ],
            "order_by": [{"field": "visits.count", "direction": "desc"}],
            "limit": 50,
        },
    )

    assert compiled["params"] == ("day", "2026-06-22", "2026-06-28")
    assert compiled["members"] == ["visits.visit_date", "visits.username", "visits.count"]
    assert 'FROM "raw_visits"' in compiled["sql"]
    assert 'date_trunc(%s, "visit_date")::date AS "date"' in compiled["sql"]
    assert '"username" AS "visits__username"' in compiled["sql"]
    assert 'COUNT(*) AS "visits__count"' in compiled["sql"]
    assert 'WHERE "visit_date" BETWEEN %s AND %s' in compiled["sql"]
    assert 'ORDER BY "visits__count" DESC' in compiled["sql"]
    assert compiled["sql"].endswith("LIMIT 50")


def test_compile_rejects_unknown_member(monkeypatch, workspace, semantic_model):
    monkeypatch.setattr(query_service, "ensure_semantic_model", lambda _workspace: semantic_model)

    with pytest.raises(query_service.SemanticQueryError, match="Unknown semantic field"):
        query_service._compile_semantic_query(
            workspace,
            {"measures": ["visits.missing"]},
        )


def test_compile_rejects_cross_dataset_query(monkeypatch, workspace, semantic_model):
    users = SemanticDataset.objects.create(
        semantic_model=semantic_model,
        workspace=workspace,
        name="users",
        label="Users",
        table_name="raw_users",
        schema_name="tenant_schema",
    )
    SemanticField.objects.create(
        dataset=users,
        name="username",
        label="Username",
        field_type=SemanticField.FieldType.DIMENSION,
        data_type="text",
        expression="username",
    )
    monkeypatch.setattr(query_service, "ensure_semantic_model", lambda _workspace: semantic_model)

    with pytest.raises(query_service.SemanticQueryError, match="one dataset"):
        query_service._compile_semantic_query(
            workspace,
            {
                "measures": ["visits.count"],
                "dimensions": ["users.username"],
            },
        )


def test_generate_cube_schema_from_semantic_model(semantic_model):
    schema = generate_cube_schema(semantic_model)

    assert schema["model"]["version"] == 1
    cube = schema["cubes"][0]
    assert cube["name"] == "visits"
    assert cube["sql_table"] == '"tenant_schema"."raw_visits"'
    assert {"name": "count", "type": "count"} in cube["measures"]
    assert {
        "name": "username",
        "sql": '{CUBE}."username"',
        "type": "string",
    } in cube["dimensions"]
    assert {
        "name": "visit_date",
        "sql": '{CUBE}."visit_date"',
        "type": "time",
    } in cube["dimensions"]
