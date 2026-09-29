"""All SQL-bearing Cube properties share one literal/reference boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from apps.semantic.models import (
    CubeSchema,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services import cube_schema
from apps.semantic.services.catalog import serialize_catalog
from apps.semantic.services.cube import generate_cube_schema
from apps.semantic.services.cube_schema import CubeSchemaBuildError, build_and_promote_cube_schema
from apps.semantic.services.cube_sql import embed_cube_sql
from apps.semantic.services.custom_datasets import compile_custom_dataset_sql


@pytest.mark.parametrize(
    "value",
    ["'{CUBE}'", "'{% jinja %}'", "'{{jinja}}'", "'[0-9]{2}'", '"column{count}"', "-- {count}\n1"],
)
def test_literals_and_comments_never_become_references(value):
    assert embed_cube_sql(value, references={"CUBE", "count"}) == embed_cube_sql(value)
    assert "{" not in embed_cube_sql(value)


def test_only_unquoted_explicit_references_are_interpolated():
    source = "{count} / NULLIF({visits.count}, 0) + LENGTH('{count}')"
    assert embed_cube_sql(source, references={"count", "visits.count"}) == (
        r"{count} / NULLIF({visits.count}, 0) + LENGTH('\u007bcount\u007d')"
    )


def test_template_delimiters_and_existing_unicode_escape_are_inert_in_default_ci():
    assert embed_cube_sql("'`${CUBE}`'") == r"'\u0060\u0024\u007bCUBE\u007d\u0060'"
    assert embed_cube_sql(r"'\u007b'") == r"'\\u007b'"


@pytest.mark.parametrize("value", ["{unknown}", "{CUBE.constructor}", "{CUBE", "{count()}", "1}"])
def test_unknown_unquoted_references_fail_closed(value):
    with pytest.raises(ValueError, match="Cube SQL reference"):
        embed_cube_sql(value, references={"CUBE", "count"})


def test_invalid_sql_text_and_unterminated_empty_reference_are_domain_errors():
    with pytest.raises(ValueError, match="Invalid SQL text"):
        embed_cube_sql("'{unclosed}", references={"CUBE"})
    with pytest.raises(ValueError, match="Unterminated Cube SQL reference"):
        embed_cube_sql("{", references={""})


@pytest.mark.django_db
def test_schema_embeds_custom_sql_physical_columns_measures_filters_and_joins(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Embedding contract")
    raw = SemanticDataset.objects.create(
        workspace=workspace,
        semantic_model=model,
        name="raw_visits",
        table_name="raw{visits}",
        primary_key="id",
    )
    for name, expression in [("id", "id"), ("topic", "topic{count}")]:
        SemanticField.objects.create(
            dataset=raw, name=name, expression=expression, field_type="dimension"
        )
    SemanticField.objects.create(
        dataset=raw, name="count", field_type="measure", measure_type="count"
    )
    SemanticField.objects.create(
        dataset=raw,
        name="ratio",
        field_type="measure",
        measure_type="number",
        metadata={
            "cube_sql": "{CUBE.count}::numeric / NULLIF({raw_visits.count}, 0)",
            "filters": [{"sql": "{CUBE}.\"topic\" = '{count}' /* {unknown} */"}],
        },
    )
    compiled = compile_custom_dataset_sql(
        "SELECT form_json #>> '{form,topic}' AS topic FROM raw_visits",
        allowed_tables={"raw_visits": "raw{visits}"},
    )
    custom = SemanticDataset.objects.create(
        workspace=workspace,
        semantic_model=model,
        name="topics",
        source_kind="custom",
        metadata={"cube_sql": compiled},
    )
    SemanticField.objects.create(
        dataset=custom, name="topic", expression="topic", field_type="dimension"
    )
    SemanticRelationship.objects.create(
        workspace=workspace,
        name="topic_relation",
        from_dataset=raw,
        to_dataset=custom,
        relationship_type="many_to_one",
        join_expression="{raw_visits.topic} = {topics.topic} /* {unknown} */",
    )

    cubes = {cube["name"]: cube for cube in generate_cube_schema(model)["cubes"]}
    assert "{form,topic}" in compiled  # The PostgreSQL probe still receives plain SQL.
    custom.refresh_from_db()
    assert custom.metadata["cube_sql"] == compiled
    assert r"'\u007bform,topic\u007d'" in cubes["topics"]["sql"]
    assert r'"raw\u007bvisits\u007d"' in cubes["raw_visits"]["sql"]
    assert "{SECURITY_CONTEXT.cubeDataRevision}" in cubes["topics"]["sql"]
    dimensions = {field["name"]: field for field in cubes["raw_visits"]["dimensions"]}
    assert dimensions["topic"]["sql"] == r'{CUBE}."topic\u007bcount\u007d"'
    ratio = next(field for field in cubes["raw_visits"]["measures"] if field["name"] == "ratio")
    assert ratio["sql"] == "{CUBE.count}::numeric / NULLIF({raw_visits.count}, 0)"
    assert ratio["filters"][0]["sql"] == (
        r"""{CUBE}."topic" = '\u007bcount\u007d' /* \u007bunknown\u007d */"""
    )
    assert cubes["raw_visits"]["joins"][0]["sql"] == (
        r"{raw_visits.topic} = {topics.topic} /* \u007bunknown\u007d */"
    )
    custom.fields.update(is_visible=False)
    cubes = {cube["name"]: cube for cube in generate_cube_schema(model)["cubes"]}
    assert "joins" not in cubes["raw_visits"]
    custom.fields.update(is_visible=True)
    custom.metadata = {}
    custom.save(update_fields=["metadata"])
    cubes = {cube["name"]: cube for cube in generate_cube_schema(model)["cubes"]}
    assert set(cubes) == {"raw_visits"}
    assert "joins" not in cubes["raw_visits"]
    custom.metadata = {"cube_sql": compiled}
    custom.is_visible = False
    custom.save(update_fields=["metadata", "is_visible"])
    schema = generate_cube_schema(model)
    assert len(schema["cubes"]) == 1
    assert "joins" not in schema["cubes"][0]
    raw.is_visible = False
    raw.save(update_fields=["is_visible"])
    assert generate_cube_schema(model)["cubes"] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("broken_part", ["measure", "filter", "malformed_sql"])
def test_invalid_sql_is_a_typed_build_failure_and_preserves_active_schema(workspace, broken_part):
    model = SemanticModel.objects.create(
        workspace=workspace, name="Last known good", status="active"
    )
    dataset = SemanticDataset.objects.create(
        workspace=workspace,
        semantic_model=model,
        name="visits",
        table_name="raw_visits",
        primary_key="id",
    )
    metadata = {"cube_sql": "{missing}"}
    if broken_part == "filter":
        metadata = {"filters": [{"sql": "{missing} = 1"}]}
    elif broken_part == "malformed_sql":
        metadata = {"cube_sql": "'unterminated"}
    SemanticField.objects.create(
        dataset=dataset, name="count", field_type="measure", measure_type="count", metadata=metadata
    )
    active = CubeSchema.objects.create(
        workspace=workspace,
        semantic_model=model,
        filename="previous.yaml",
        content="old",
        content_hash="previous",
        status=CubeSchema.Status.ACTIVE,
    )

    with pytest.raises(CubeSchemaBuildError, match="Could not generate Cube schema"):
        build_and_promote_cube_schema(workspace, model=model)

    active.refresh_from_db()
    model.refresh_from_db()
    assert active.status == CubeSchema.Status.ACTIVE
    assert active.content == "old"
    assert model.status == SemanticModel.Status.ACTIVE
    assert model.metadata["last_build"]["ok"] is False
    assert model.diagnostics


def _stale_relationship_catalog(workspace) -> SemanticModel:
    model = SemanticModel.objects.create(workspace=workspace, name="Stale join", status="active")
    datasets = {}
    for name in ("visits", "users", "cases"):
        datasets[name] = SemanticDataset.objects.create(
            workspace=workspace,
            semantic_model=model,
            name=name,
            table_name=f"raw_{name}",
            primary_key="id",
        )
        for field in ("id", "user_id"):
            SemanticField.objects.create(
                dataset=datasets[name], name=field, expression=field, field_type="dimension"
            )
    SemanticRelationship.objects.create(
        workspace=workspace,
        name="visits_to_users",
        from_dataset=datasets["visits"],
        to_dataset=datasets["users"],
        relationship_type="many_to_one",
        join_expression="{visits.user_id} = {users.id}",
    )
    return model


@pytest.mark.django_db
def test_valid_relationships_build_without_diagnostics(workspace):
    schema = generate_cube_schema(_stale_relationship_catalog(workspace))

    assert schema["diagnostics"] == []
    cubes = {cube["name"]: cube for cube in schema["cubes"]}
    assert cubes["visits"]["joins"] == [
        {"name": "users", "relationship": "many_to_one", "sql": "{visits.user_id} = {users.id}"}
    ]


@pytest.mark.django_db(transaction=True)
def test_stale_relationship_is_skipped_and_reported_without_failing_the_build(
    workspace, monkeypatch
):
    model = _stale_relationship_catalog(workspace)
    visits, cases = model.datasets.get(name="visits"), model.datasets.get(name="cases")
    SemanticRelationship.objects.create(
        workspace=workspace,
        name="cases_to_visits",
        from_dataset=cases,
        to_dataset=visits,
        relationship_type="many_to_one",
        join_expression="{cases.renamed_visit_id} = {visits.id}",
    )
    monkeypatch.setattr(
        cube_schema.CubeClient, "validate_schema", AsyncMock(return_value={"valid": True})
    )
    monkeypatch.setattr(cube_schema.CubeClient, "invalidate_schema_cache", AsyncMock())
    monkeypatch.setattr(
        cube_schema,
        "load_workspace_context",
        AsyncMock(return_value=SimpleNamespace(schema_name="fixture", readonly_role="role")),
    )

    active = build_and_promote_cube_schema(workspace, model=model)

    cubes = {cube["name"]: cube for cube in yaml.safe_load(active.content)["cubes"]}
    assert set(cubes) == {"visits", "users", "cases"}
    assert cubes["visits"]["joins"][0]["name"] == "users"
    assert "joins" not in cubes["cases"]
    model.refresh_from_db()
    assert model.metadata["last_build"]["ok"] is True
    stale = [d for d in model.diagnostics if d.get("code") == "relationship_stale_reference"]
    assert (
        stale
        == active.diagnostics
        == [
            {
                "level": "warning",
                "code": "relationship_stale_reference",
                "relationship": "cases_to_visits",
                "message": (
                    "Relationship 'cases_to_visits' was not published: it references "
                    "'cases.renamed_visit_id', which is not in the semantic catalog."
                ),
            }
        ]
    )
    catalog = {dataset["name"]: dataset for dataset in serialize_catalog(model)["datasets"]}
    published = {r["name"]: r.get("published", True) for r in catalog["visits"]["relationships"]}
    assert published == {"visits_to_users": True, "cases_to_visits": False}


@pytest.mark.django_db
def test_relationship_through_hidden_member_is_skipped_and_reported(workspace):
    model = _stale_relationship_catalog(workspace)
    SemanticField.objects.filter(dataset__name="users", name="id").update(is_visible=False)

    schema = generate_cube_schema(model)

    cubes = {cube["name"]: cube for cube in schema["cubes"]}
    assert "joins" not in cubes["visits"]
    assert [(d["code"], d["relationship"]) for d in schema["diagnostics"]] == [
        ("relationship_hidden_reference", "visits_to_users")
    ]


@pytest.mark.django_db
def test_relationship_to_hidden_dataset_is_reported_as_unpublished(workspace):
    model = _stale_relationship_catalog(workspace)
    # Catalog refresh hides the dataset of a source table that disappeared.
    SemanticDataset.objects.filter(name="users").update(is_visible=False)

    schema = generate_cube_schema(model)

    assert "joins" not in {c["name"]: c for c in schema["cubes"]}["visits"]
    assert [(d["code"], d["relationship"]) for d in schema["diagnostics"]] == [
        ("relationship_unpublished_endpoint", "visits_to_users")
    ]
    assert "'users'" in schema["diagnostics"][0]["message"]


@pytest.mark.django_db
def test_relationship_between_hidden_datasets_is_not_reported(workspace):
    model = _stale_relationship_catalog(workspace)
    SemanticDataset.objects.filter(name__in=["visits", "users"]).update(is_visible=False)

    assert generate_cube_schema(model)["diagnostics"] == []


@pytest.mark.django_db
def test_relationship_from_dataset_without_primary_key_is_reported(workspace):
    model = _stale_relationship_catalog(workspace)
    SemanticDataset.objects.filter(name="visits").update(primary_key="")

    schema = generate_cube_schema(model)

    assert "joins" not in {c["name"]: c for c in schema["cubes"]}["visits"]
    assert [(d["code"], d["relationship"]) for d in schema["diagnostics"]] == [
        ("relationship_missing_primary_key", "visits_to_users")
    ]
