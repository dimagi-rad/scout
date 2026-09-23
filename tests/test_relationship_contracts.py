"""Provider-shaped relationships run against real PostgreSQL, not string snapshots."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.db import connection

from apps.semantic.models import SemanticDataset, SemanticModel, SemanticRelationship
from apps.semantic.services import catalog, cube_schema
from mcp_server.pipeline_registry import (
    PipelineRegistry,
    RelationshipConfig,
    SourceConfig,
    _parse_pipeline,
)
from mcp_server.services.materializer import _write_forms
from mcp_server.services.metadata import pipeline_list_tables

pytestmark = pytest.mark.django_db


def _dataset(model, name, columns, primary_key="id", *, source=None):
    dataset = SemanticDataset.objects.create(
        workspace=model.workspace,
        semantic_model=model,
        name=name,
        table_name=name,
        primary_key=primary_key,
        metadata=source or {},
    )
    catalog._sync_fields(
        dataset, [{"name": column, "type": kind} for column, kind in columns], None
    )
    return dataset


def test_hq_forms_use_a_distinct_association_bridge(workspace):
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA relationship_hq_test")
    rows = [
        {"form_id": "f1", "case_ids": ["c1", "c2", "c1", None, 5, ""]},
        {"form_id": "f2", "case_ids": ["c1"]},
        {"form_id": "f3", "case_ids": None},
        {"form_id": "f4", "case_ids": {"invalid": "not an array"}},
    ]
    assert _write_forms(iter([(rows, len(rows))]), "relationship_hq_test", connection) == 4
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT form_id, case_id FROM relationship_hq_test.raw_form_cases ORDER BY 1, 2"
        )
        assert cursor.fetchall() == [("f1", "c1"), ("f1", "c2"), ("f2", "c1")]
        cursor.execute(
            "SELECT count(DISTINCT form_case_id) FROM relationship_hq_test.raw_form_cases"
        )
        assert cursor.fetchone()[0] == 3

    model = SemanticModel.objects.create(workspace=workspace, name="HQ")
    _dataset(model, "raw_forms", [("form_id", "text"), ("case_ids", "jsonb")], "form_id")
    _dataset(
        model,
        "raw_form_cases",
        [("form_case_id", "text"), ("form_id", "text"), ("case_id", "text")],
        "form_case_id",
    )
    _dataset(model, "raw_cases", [("case_id", "text")], "case_id")
    assert catalog._sync_relationships(model, workspace) == []
    relationships = list(SemanticRelationship.objects.filter(workspace=workspace).order_by("name"))
    assert {r.relationship_type for r in relationships} == {"one_to_many", "many_to_one"}
    assert all("case_ids}" not in r.join_expression for r in relationships)
    assert len(relationships) == 2

    # A later refresh replaces, rather than accumulates, the association snapshot.
    _write_forms(
        iter([([{"form_id": "f1", "case_ids": ["c3"]}], 1)]), "relationship_hq_test", connection
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT form_id, case_id FROM relationship_hq_test.raw_form_cases")
        assert cursor.fetchall() == [("f1", "c3")]


def test_ocs_ambiguous_identity_cannot_fan_out(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="OCS")
    _dataset(
        model,
        "raw_sessions",
        [("session_id", "text"), ("participant_identifier", "text")],
        "session_id",
    )
    _dataset(
        model,
        "raw_participants",
        [("participant_id", "text"), ("identifier", "text"), ("platform", "text")],
        "participant_id",
    )
    catalog._sync_relationships(model, workspace)
    relationship = SemanticRelationship.objects.get(workspace=workspace)
    predicate = relationship.join_expression.replace(
        "{raw_sessions.participant_identifier}", "s.participant_identifier"
    ).replace("{raw_participants.identifier}", "p.identifier")
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE raw_participants (participant_id text, identifier text, platform text)"
        )
        cursor.execute(
            "INSERT INTO raw_participants VALUES ('p1', 'duplicate', 'web'), ('p2', 'duplicate', 'api'), ('p3', 'unique', 'web')"
        )
        cursor.execute(
            "SELECT s.session_id, p.participant_id FROM (VALUES ('s1', 'duplicate'), ('s2', 'unique')) s(session_id, participant_identifier) LEFT JOIN raw_participants p ON "
            + predicate
            + " ORDER BY 1"
        )
        assert cursor.fetchall() == [("s1", None), ("s2", "p3")]
    assert relationship.metadata["require_unique_target"] is True


def test_composite_keys_keep_tenant_scope_and_declared_cardinality(workspace, monkeypatch):
    model = SemanticModel.objects.create(workspace=workspace, name="Composite")

    def source(owner, table):
        return {"source_tenant_ids": [owner], "source_table_name": table}

    _dataset(
        model,
        "fitted_a_938ac",
        [("id", "text"), ("platform", "text")],
        source=source("tenant-a", "rows"),
    )
    _dataset(
        model,
        "fitted_b_f712a",
        [("id", "text"), ("platform", "text")],
        source=source("tenant-a", "identities"),
    )
    _dataset(
        model,
        "unrelated_tenant",
        [("id", "text"), ("platform", "text")],
        source=source("tenant-b", "identities"),
    )
    relation = RelationshipConfig(
        from_table="rows",
        from_column="id",
        to_table="identities",
        to_column="id",
        additional_keys=[("platform", "platform")],
        relationship_type="many_to_one",
    )
    monkeypatch.setattr(
        catalog,
        "get_registry",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(relationships=[relation])]),
    )
    catalog._sync_relationships(model, workspace)
    relationship = SemanticRelationship.objects.get(workspace=workspace)
    assert relationship.relationship_type == "many_to_one"  # Not inferred from the from-PK.
    assert relationship.to_dataset.name == "fitted_b_f712a"
    assert relationship.join_expression == (
        "{fitted_a_938ac.id} = {fitted_b_f712a.id} AND {fitted_a_938ac.platform} = {fitted_b_f712a.platform}"
    )


def test_non_scalar_keys_are_not_promoted_as_equality_joins(workspace, monkeypatch):
    model = SemanticModel.objects.create(workspace=workspace, name="Invalid")
    _dataset(model, "forms", [("id", "text"), ("case_ids", "jsonb")])
    _dataset(model, "cases", [("id", "text")])
    relation = RelationshipConfig(
        from_table="forms", from_column="case_ids", to_table="cases", to_column="id"
    )
    monkeypatch.setattr(
        catalog,
        "get_registry",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(relationships=[relation])]),
    )
    diagnostics = catalog._sync_relationships(model, workspace)
    assert diagnostics[0]["code"] == "relationship_key_type"
    assert not SemanticRelationship.objects.filter(workspace=workspace).exists()


def test_all_pipeline_relationships_have_deliberate_cardinality():
    registry = PipelineRegistry()
    assert not registry.load_errors
    assert registry.get("commcare_sync").sources[1].auxiliary_tables["raw_form_cases"]
    assert registry.get("ocs_sync").relationships[-1].require_unique_target is True
    assert all(
        r.relationship_type == "many_to_one" for r in registry.get("connect_sync").relationships
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,exists,expected",
    [("completed", True, True), ("completed", False, False), ("failed", True, False)],
)
async def test_auxiliary_tables_require_completed_source_and_physical_presence(
    state, exists, expected
):
    pipeline = PipelineRegistry().get("commcare_sync")
    run = SimpleNamespace(
        completed_at=None, result={"sources": {"forms": {"state": state, "rows": 8}}}
    )
    with (
        patch("mcp_server.services.metadata.MaterializationRun") as runs,
        patch(
            "mcp_server.services.metadata._live_tables_in_schema",
            AsyncMock(return_value={"raw_forms", "raw_form_cases"} if exists else {"raw_forms"}),
        ),
    ):
        runs.objects.filter.return_value.order_by.return_value.afirst = AsyncMock(return_value=run)
        tables = await pipeline_list_tables(SimpleNamespace(schema_name="fixture"), pipeline)
    bridge = next((table for table in tables if table["name"] == "raw_form_cases"), None)
    assert (bridge is not None) is expected
    if bridge:
        assert bridge["materialized_row_count"] is None


def test_refresh_preserves_hand_authored_relationship_with_generated_name(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Curated")
    visits = _dataset(
        model, "raw_visits", [("visit_id", "integer"), ("username", "text")], "visit_id"
    )
    users = _dataset(model, "raw_users", [("username", "text")], "username")
    relationship = SemanticRelationship.objects.create(
        workspace=workspace,
        name="raw_visits_username_to_raw_users",
        from_dataset=visits,
        to_dataset=users,
        relationship_type="one_to_one",
        join_expression="curated expression",
        metadata={"source": "canvas"},
    )
    catalog._sync_relationships(model, workspace)
    relationship.refresh_from_db()
    assert relationship.join_expression == "curated expression"
    assert relationship.relationship_type == "one_to_one"


def test_empty_optional_yaml_collections_are_normalized():
    pipeline = _parse_pipeline(
        {
            "pipeline": "example",
            "provider": "commcare",
            "sources": [{"name": "forms", "auxiliary_tables": None}],
            "relationships": [
                {
                    "from_table": "a",
                    "from_column": "id",
                    "to_table": "b",
                    "to_column": "id",
                    "additional_keys": None,
                }
            ],
        }
    )
    assert pipeline.sources[0].auxiliary_tables == {}
    assert pipeline.relationships[0].key_pairs == [("id", "id")]


@pytest.mark.parametrize("value", [None, [], "raw_forms", {"raw_forms": 1}])
def test_invalid_auxiliary_table_contract_is_rejected(value):
    with pytest.raises(ValueError, match="auxiliary_tables"):
        SourceConfig(name="forms", auxiliary_tables=value)


@pytest.mark.django_db(transaction=True)
def test_catalog_warnings_survive_cube_promotion(workspace, monkeypatch):
    monkeypatch.setattr(
        catalog,
        "load_physical_tables",
        lambda workspace: (
            "fixture",
            [
                catalog.PhysicalTable(
                    "raw_visits", "table", "", [{"name": "username", "type": "jsonb"}]
                ),
                catalog.PhysicalTable(
                    "raw_users", "table", "", [{"name": "username", "type": "text"}]
                ),
            ],
        ),
    )
    model = catalog.ensure_semantic_model(workspace)
    warnings = model.diagnostics
    assert warnings[0]["code"] == "relationship_key_type"
    assert warnings[0]["level"] == "warning"
    monkeypatch.setattr(
        cube_schema.CubeClient, "validate_schema", AsyncMock(return_value={"valid": True})
    )
    monkeypatch.setattr(cube_schema.CubeClient, "invalidate_schema_cache", AsyncMock())
    monkeypatch.setattr(
        cube_schema,
        "load_workspace_context",
        AsyncMock(
            return_value=SimpleNamespace(schema_name="fixture", readonly_role="fixture_role")
        ),
    )
    cube_schema.build_and_promote_cube_schema(workspace, model=model)
    model.refresh_from_db()
    assert model.diagnostics == warnings
    assert model.metadata["catalog_diagnostics"] == warnings
