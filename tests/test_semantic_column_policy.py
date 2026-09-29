"""Tenant-constant ids and raw JSON stay queryable but out of catalog listings (#359)."""

from unittest.mock import MagicMock

import pytest
from asgiref.sync import sync_to_async

from apps.semantic.models import SemanticField
from apps.semantic.services import catalog as catalog_service
from apps.semantic.services import sample_rows as sample_rows_service
from apps.semantic.services.catalog import PhysicalTable
from apps.semantic.services.column_policy import RAW_JSON, TENANT_CONSTANT_ID
from apps.semantic.services.cube import generate_cube_schema
from mcp_server.pipeline_registry import RelationshipConfig
from mcp_server.server import describe_dataset, list_datasets

SESSIONS_TO_EXPERIMENTS = RelationshipConfig(
    from_table="raw_sessions",
    from_column="experiment_id",
    to_table="raw_experiments",
    to_column="experiment_id",
    description="Sessions belong to the chatbot",
)


def _ocs_tables(provider="ocs"):
    return [
        PhysicalTable(
            name="raw_sessions",
            type="table",
            description="Sessions",
            columns=[
                {"name": "session_id", "type": "text"},
                {"name": "experiment_id", "type": "text"},
                {"name": "participant_identifier", "type": "text"},
                {"name": "tags", "type": "jsonb"},
            ],
            primary_key="session_id",
            provider=provider,
        ),
        PhysicalTable(
            name="raw_experiments",
            type="table",
            description="Chatbots",
            columns=[
                {"name": "experiment_id", "type": "text"},
                {"name": "name", "type": "text"},
            ],
            primary_key="experiment_id",
            provider=provider,
        ),
    ]


def _connect_visits():
    return PhysicalTable(
        name="raw_visits",
        type="table",
        description="Visits",
        columns=[
            {"name": "visit_id", "type": "bigint"},
            {"name": "opportunity_id", "type": "bigint"},
            {"name": "username", "type": "text"},
            {"name": "form_json", "type": "jsonb"},
        ],
        primary_key="visit_id",
        provider="commcare_connect",
    )


def _build(monkeypatch, workspace, tables, relationships=()):
    pipeline = MagicMock()
    pipeline.relationships = list(relationships)
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    monkeypatch.setattr(
        catalog_service, "load_physical_tables", lambda _workspace: ("tenant_schema", tables)
    )
    monkeypatch.setattr(catalog_service, "get_registry", lambda: registry)
    return catalog_service.ensure_semantic_model(workspace)


def _listed_members(catalog, dataset_name):
    entry = next(d for d in catalog["datasets"] if d["name"] == dataset_name)
    return {f["name"] for f in entry["dimensions"] + entry["time_dimensions"] + entry["measures"]}


def test_catalog_omits_tenant_constant_ids_and_raw_json(monkeypatch, workspace):
    model = _build(monkeypatch, workspace, [*_ocs_tables(), _connect_visits()])

    catalog = catalog_service.serialize_catalog(model)

    assert _listed_members(catalog, "raw_sessions") == {
        "count",
        "session_id",
        "participant_identifier",
    }
    assert _listed_members(catalog, "raw_experiments") == {"count", "name"}
    assert _listed_members(catalog, "raw_visits") == {"count", "visit_id", "username"}
    visits = model.datasets.get(name="raw_visits")
    assert visits.fields.get(name="opportunity_id").metadata["catalog_hidden"] == (
        TENANT_CONSTANT_ID
    )
    assert visits.fields.get(name="form_json").metadata["catalog_hidden"] == RAW_JSON


def test_hidden_columns_stay_queryable_and_joinable(monkeypatch, workspace):
    model = _build(monkeypatch, workspace, _ocs_tables(), [SESSIONS_TO_EXPERIMENTS])

    schema = generate_cube_schema(model)

    assert schema["diagnostics"] == []
    sessions = next(c for c in schema["cubes"] if c["name"] == "raw_sessions")
    assert {"experiment_id", "tags"} <= {d["name"] for d in sessions["dimensions"]}
    assert sessions["joins"] == [
        {
            "name": "raw_experiments",
            "relationship": "many_to_one",
            "sql": "{raw_sessions.experiment_id} = {raw_experiments.experiment_id}",
        }
    ]
    catalog = catalog_service.serialize_catalog(model)
    sessions_entry = next(d for d in catalog["datasets"] if d["name"] == "raw_sessions")
    assert [r["to_dataset"] for r in sessions_entry["relationships"]] == ["raw_experiments"]
    assert "published" not in sessions_entry["relationships"][0]


def test_curated_measure_over_hidden_column_survives_rebuild(monkeypatch, workspace):
    model = _build(monkeypatch, workspace, [_connect_visits()])
    visits = model.datasets.get(name="raw_visits")
    SemanticField.objects.create(
        dataset=visits,
        name="opportunities",
        label="Opportunities",
        field_type=SemanticField.FieldType.MEASURE,
        data_type="integer",
        expression="opportunity_id",
        measure_type=SemanticField.MeasureType.COUNT_DISTINCT,
        metadata={"source": "canvas", "cube_sql": "{CUBE.opportunity_id}"},
    )

    model = _build(monkeypatch, workspace, [_connect_visits()])

    cube = next(c for c in generate_cube_schema(model)["cubes"] if c["name"] == "raw_visits")
    measure = next(m for m in cube["measures"] if m["name"] == "opportunities")
    assert measure["sql"] == "{CUBE.opportunity_id}"
    assert "opportunities" in _listed_members(
        catalog_service.serialize_catalog(model), "raw_visits"
    )


def test_ids_stay_listed_when_the_owning_provider_is_unknown(monkeypatch, workspace):
    # A view unioning several tenants has no single provider; the id then tells rows apart.
    model = _build(monkeypatch, workspace, _ocs_tables(provider=""))

    catalog = catalog_service.serialize_catalog(model)

    assert "experiment_id" in _listed_members(catalog, "raw_sessions")
    assert "tags" not in _listed_members(catalog, "raw_sessions")


def test_default_sample_skips_hidden_columns(monkeypatch, workspace):
    _build(monkeypatch, workspace, [_connect_visits()])
    captured = {}

    def fake_run_semantic_query(_workspace, query_spec):
        captured["query_spec"] = query_spec
        return {"columns": [], "rows": [], "row_count": 0}

    monkeypatch.setattr(sample_rows_service, "run_semantic_query_sync", fake_run_semantic_query)

    sample_rows_service.sample_dataset_rows(workspace, "raw_visits")

    assert sorted(captured["query_spec"]["dimensions"]) == [
        "raw_visits.username",
        "raw_visits.visit_id",
    ]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_discovery_tools_omit_hidden_columns(monkeypatch, workspace, user):
    await sync_to_async(_build)(monkeypatch, workspace, [_connect_visits()])

    listed = await list_datasets(user_id=str(user.id), include_fields=True)
    described = await describe_dataset(
        "raw_visits", workspace_id=str(workspace.id), user_id=str(user.id)
    )

    assert listed["success"] is True, listed
    fields = {f["name"] for f in listed["data"]["datasets"][0]["fields"]}
    assert fields == {"count", "visit_id", "username"}
    assert described["success"] is True, described
    dataset = described["data"]["dataset"]
    assert {f["name"] for f in dataset["dimensions"]} == {"visit_id", "username"}
