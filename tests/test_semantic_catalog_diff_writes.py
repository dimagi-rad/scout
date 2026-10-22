"""A catalog refresh writes only the rows whose derived values changed (#918)."""

import dataclasses
from unittest.mock import MagicMock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.semantic.models import SemanticDataset, SemanticField, SemanticRelationship
from apps.semantic.services import catalog as catalog_service
from apps.semantic.services.catalog import PhysicalTable
from mcp_server.pipeline_registry import RelationshipConfig

WIDE_COLUMNS = 200

VISITS_TO_USERS = RelationshipConfig(
    from_table="raw_visits",
    from_column="user_id",
    to_table="raw_users",
    to_column="id",
    description="Visits belong to a user",
    additional_keys=[("org_id", "org_id")],
)


def _columns(overrides=None, *, drop=(), extra=()):
    overrides = overrides or {}
    columns = [
        {"name": "id", "type": "text"},
        {"name": "user_id", "type": "text"},
        {"name": "org_id", "type": "text"},
        *(
            {"name": f"col_{index}", "type": "bigint" if index % 3 == 0 else "text"}
            for index in range(WIDE_COLUMNS)
        ),
        *extra,
    ]
    return [
        {**column, **overrides.get(column["name"], {})}
        for column in columns
        if column["name"] not in drop
    ]


def _tables(visit_columns, *, materialized_at="2026-10-22T00:00:00Z"):
    return [
        PhysicalTable(
            name="raw_visits",
            type="table",
            description="",
            columns=visit_columns,
            primary_key="id",
            materialized_at=materialized_at,
        ),
        PhysicalTable(
            name="raw_users",
            type="table",
            description="",
            columns=[
                {"name": "id", "type": "text"},
                {"name": "org_id", "type": "text"},
            ],
            primary_key="id",
            materialized_at=materialized_at,
        ),
    ]


@pytest.fixture
def refresh(monkeypatch, workspace):
    pipeline = MagicMock()
    pipeline.relationships = [VISITS_TO_USERS]
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    monkeypatch.setattr(catalog_service, "get_registry", lambda: registry)

    def run(tables, relationships=(VISITS_TO_USERS,)):
        pipeline.relationships = list(relationships)
        monkeypatch.setattr(
            catalog_service, "load_physical_tables", lambda _workspace: ("schema", tables)
        )
        return catalog_service.ensure_semantic_model(workspace)

    return run


def _writes(queries, table):
    return [
        query["sql"]
        for query in queries
        if f'"{table}"' in query["sql"] and query["sql"].split(None, 1)[0] in {"INSERT", "UPDATE"}
    ]


def _snapshot(model_class, **filters):
    return {
        row["id"]: row
        for row in model_class.objects.filter(**filters).values(
            *[field.attname for field in model_class._meta.concrete_fields]
        )
    }


def test_unchanged_refresh_writes_nothing_in_constant_queries(refresh, workspace):
    refresh(_tables(_columns()))
    fields = _snapshot(SemanticField, dataset__workspace=workspace)
    datasets = _snapshot(SemanticDataset, workspace=workspace)
    relationships = _snapshot(SemanticRelationship, workspace=workspace)
    assert len(fields) > WIDE_COLUMNS
    assert len(relationships) == 1

    with CaptureQueriesContext(connection) as ctx:
        refresh(_tables(_columns()))

    # O(1), not O(columns): one read per model plus the model row itself.
    assert len(ctx.captured_queries) <= 20
    assert _writes(ctx.captured_queries, "semantic_semanticfield") == []
    assert _writes(ctx.captured_queries, "semantic_semanticrelationship") == []
    # Only the hide-what-is-gone sweeps over physical and custom datasets remain.
    assert all(
        sql.startswith('UPDATE "semantic_semanticdataset" SET "is_visible" = false')
        for sql in _writes(ctx.captured_queries, "semantic_semanticdataset")
    )
    assert _snapshot(SemanticField, dataset__workspace=workspace) == fields
    assert _snapshot(SemanticDataset, workspace=workspace) == datasets
    assert _snapshot(SemanticRelationship, workspace=workspace) == relationships


def test_changed_column_updates_only_its_field(refresh, workspace):
    refresh(_tables(_columns()))
    before = _snapshot(SemanticField, dataset__workspace=workspace)

    with CaptureQueriesContext(connection) as ctx:
        refresh(_tables(_columns({"col_1": {"type": "date", "description": "When"}})))

    assert len(_writes(ctx.captured_queries, "semantic_semanticfield")) == 1
    after = _snapshot(SemanticField, dataset__workspace=workspace)
    changed = {pk for pk in after if after[pk] != before[pk]}
    field = SemanticField.objects.get(pk=changed.pop())
    assert not changed
    assert field.name == "col_1"
    assert field.data_type == "date"
    assert field.description == "When"
    assert field.field_type == SemanticField.FieldType.TIME_DIMENSION
    assert field.updated_at > before[field.pk]["updated_at"]


def test_new_column_inserts_and_dropped_fields_follow_the_old_sweep(refresh, workspace):
    """Hide exactly what ``exclude(metadata__source="canvas")`` hid, NULL logic included."""
    refresh(_tables(_columns()))
    visits = SemanticDataset.objects.get(workspace=workspace, name="raw_visits")
    for name, metadata in (
        ("legacy_no_source", {}),
        ("legacy_canvas", {"source": "canvas"}),
        ("legacy_import", {"source": "import"}),
        ("legacy_json_null", {"source": None}),
    ):
        SemanticField.objects.create(
            dataset=visits,
            name=name,
            field_type=SemanticField.FieldType.DIMENSION,
            expression=name,
            metadata=metadata,
        )
    stale = {"col_3", "sum_col_3", "avg_col_3"} | {
        name for name in visits.fields.values_list("name", flat=True) if name.startswith("legacy_")
    }
    old_sweep = set(
        visits.fields.filter(name__in=stale)
        .exclude(metadata__source="canvas")
        .values_list("name", flat=True)
    )
    before = _snapshot(SemanticField, dataset__workspace=workspace)

    refresh(_tables(_columns(drop={"col_3"}, extra=[{"name": "added", "type": "text"}])))

    by_name = {field.name: field for field in visits.fields.all()}
    assert by_name["added"].is_visible
    assert by_name["added"].expression == "added"
    hidden = {name for name, field in by_name.items() if not field.is_visible}
    # Fields with no ``source`` (dropped columns included) stay visible, as on main.
    assert hidden == old_sweep == {"legacy_import", "legacy_json_null"}
    for name in stale:
        # A plain hide, as before: it does not count as an edit of the field.
        assert by_name[name].updated_at == before[by_name[name].pk]["updated_at"]


@pytest.mark.parametrize("source", ["canvas", "import", None])
def test_curated_field_keeps_its_curation(refresh, workspace, source):
    refresh(_tables(_columns()))
    field = SemanticField.objects.get(dataset__workspace=workspace, name="col_2")
    field.label = "Curated label"
    field.description = "Curated description"
    field.metadata = {
        **field.metadata,
        "curated_fields": ["description", "format", "label"],
        "format": "percent",
        **({"source": source} if source else {}),
    }
    field.save()
    curated = _snapshot(SemanticField, pk=field.pk)[field.pk]

    refresh(_tables(_columns()))
    assert _snapshot(SemanticField, pk=field.pk)[field.pk] == curated

    refresh(_tables(_columns({"col_2": {"description": "From the warehouse", "type": "date"}})))
    field.refresh_from_db()
    assert field.is_visible
    assert field.label == "Curated label"
    assert field.description == "Curated description"
    assert field.data_type == "date"
    assert field.metadata["curated_fields"] == ["description", "format", "label"]
    assert field.metadata["format"] == "percent"
    assert field.metadata.get("source") == source


def test_json_change_between_equal_python_values_is_written(refresh, workspace):
    """1 == 1.0 == True in Python, but jsonb stores each differently."""
    refresh(_tables(_columns()))
    field = SemanticField.objects.get(dataset__workspace=workspace, name="col_1")
    stored = dict(field.metadata)
    assert stored["nullable"] is None
    SemanticField.objects.filter(pk=field.pk).update(metadata={**stored, "nullable": 1})

    refresh(_tables(_columns({"col_1": {"nullable": True}})))

    field.refresh_from_db()
    assert field.metadata["nullable"] is True


def test_changed_relationship_updates_only_that_row(refresh, workspace):
    refresh(_tables(_columns()))
    before = SemanticRelationship.objects.get(workspace=workspace)

    with CaptureQueriesContext(connection) as ctx:
        refresh(
            _tables(_columns()),
            [dataclasses.replace(VISITS_TO_USERS, description="Each visit's user")],
        )

    assert len(_writes(ctx.captured_queries, "semantic_semanticrelationship")) == 1
    after = SemanticRelationship.objects.get(workspace=workspace)
    assert after.pk == before.pk
    assert after.metadata == {**before.metadata, "description": "Each visit's user"}
    assert after.join_expression == before.join_expression
    assert after.updated_at > before.updated_at


def test_refresh_locks_the_rows_it_diffs(refresh, workspace):
    """A canvas commit skips the model lock, so the rows themselves must be held."""
    refresh(_tables(_columns()))
    SemanticRelationship.objects.create(
        workspace=workspace,
        name="hand_authored",
        from_dataset=SemanticDataset.objects.get(workspace=workspace, name="raw_visits"),
        to_dataset=SemanticDataset.objects.get(workspace=workspace, name="raw_users"),
        relationship_type="many_to_one",
    )

    with CaptureQueriesContext(connection) as ctx:
        refresh(_tables(_columns()))

    locks = [
        query["sql"]
        for query in ctx.captured_queries
        if query["sql"].startswith("SELECT") and query["sql"].endswith("FOR UPDATE")
    ]
    for table, scope in (
        ("semantic_semanticfield", '"semantic_semanticfield"."dataset_id" IN'),
        ("semantic_semanticdataset", '"semantic_semanticdataset"."name" IN'),
        # Hand-authored relationships are never written, so they stay unlocked.
        ("semantic_semanticrelationship", 'NOT ("semantic_semanticrelationship"."name" IN'),
    ):
        assert any(f'FROM "{table}"' in sql and scope in sql for sql in locks), table
