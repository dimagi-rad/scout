"""All SQL-bearing Cube properties share one literal/reference boundary."""

import pytest

from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel, SemanticRelationship
from apps.semantic.services.cube import generate_cube_schema
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


@pytest.mark.parametrize("value", ["{unknown}", "{CUBE.constructor}", "{CUBE", "{count()}", "1}"])
def test_unknown_unquoted_references_fail_closed(value):
    with pytest.raises(ValueError, match="Cube SQL reference"):
        embed_cube_sql(value, references={"CUBE", "count"})


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
            "cube_sql": "{count}::numeric / NULLIF({raw_visits.count}, 0)",
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
    assert custom.metadata["cube_sql"] == compiled
    assert r"'\u007bform,topic\u007d'" in cubes["topics"]["sql"]
    assert r'"raw\u007bvisits\u007d"' in cubes["raw_visits"]["sql"]
    assert "{SECURITY_CONTEXT.cubeDataRevision}" in cubes["topics"]["sql"]
    dimensions = {field["name"]: field for field in cubes["raw_visits"]["dimensions"]}
    assert dimensions["topic"]["sql"] == r'{CUBE}."topic\u007bcount\u007d"'
    ratio = next(field for field in cubes["raw_visits"]["measures"] if field["name"] == "ratio")
    assert ratio["sql"] == "{count}::numeric / NULLIF({raw_visits.count}, 0)"
    assert ratio["filters"][0]["sql"] == (
        r"""{CUBE}."topic" = '\u007bcount\u007d' /* \u007bunknown\u007d */"""
    )
    assert cubes["raw_visits"]["joins"][0]["sql"] == (
        r"{raw_visits.topic} = {topics.topic} /* \u007bunknown\u007d */"
    )
