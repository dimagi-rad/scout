"""All SQL-bearing Cube properties share one literal/reference boundary."""

import pytest

from apps.semantic.models import (
    CubeSchema,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
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
@pytest.mark.parametrize("broken_part", ["measure", "filter", "join", "malformed_sql"])
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
    elif broken_part == "join":
        metadata = {}
        SemanticRelationship.objects.create(
            workspace=workspace,
            name="stale_join",
            from_dataset=dataset,
            to_dataset=dataset,
            relationship_type="many_to_one",
            join_expression="{visits.deleted} = {visits.id}",
        )
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
