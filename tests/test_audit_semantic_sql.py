"""The read-only audit lists stored semantic SQL that would fail publication."""

import json
from io import StringIO

import pytest
from django.core.management import call_command

from apps.semantic.models import (
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)

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


def test_audit_lists_only_stored_sql_that_fails_validation(model):
    visits = _visits(model)
    SemanticField.objects.create(
        dataset=visits,
        name="total",
        field_type="measure",
        measure_type="number",
        metadata={
            "cube_sql": "coalesce(sum({CUBE}.amount), 0)",
            "filters": [{"sql": "{CUBE}.amount > 0"}],
        },
    )
    bad_measure = SemanticField.objects.create(
        dataset=visits,
        name="escalating",
        field_type="measure",
        measure_type="number",
        is_visible=False,
        metadata={
            "cube_sql": _DENIED_SQL,
            "filters": [{"sql": "{CUBE}.amount > 0"}, {"sql": "pg_sleep(1) IS NULL"}],
        },
    )
    SemanticField.objects.create(
        dataset=visits,
        name="bad_band",
        field_type="dimension",
        metadata={"cube_sql": "current_setting('work_mem')"},
    )
    SemanticField.objects.create(
        dataset=visits,
        name="stale_ratio",
        field_type="measure",
        measure_type="number",
        metadata={"cube_sql": "{missing_count}::numeric / NULLIF({count}, 0)"},
    )
    SemanticField.objects.create(
        dataset=visits,
        name="hidden_stale_ratio",
        field_type="measure",
        measure_type="number",
        is_visible=False,
        metadata={"cube_sql": "{missing_count}::numeric / NULLIF({count}, 0)"},
    )
    SemanticField.objects.create(
        dataset=visits,
        name="broken_text",
        field_type="dimension",
        metadata={"cube_sql": "'unterminated"},
    )
    SemanticRelationship.objects.create(
        workspace=model.workspace,
        name="visits_users",
        from_dataset=visits,
        to_dataset=model.datasets.get(name="users"),
        join_expression="{visits.user_id} = {users.id}",
    )
    SemanticRelationship.objects.create(
        workspace=model.workspace,
        name="bad_join",
        from_dataset=visits,
        to_dataset=model.datasets.get(name="users"),
        join_expression="{visits.user_id} IN (SELECT oid FROM pg_class)",
    )
    before = {
        field.id: (field.metadata, field.is_visible)
        for field in SemanticField.objects.filter(dataset=visits)
    }

    out = StringIO()
    call_command("audit_semantic_sql", "--json", stdout=out)
    findings = json.loads(out.getvalue())

    assert [(f["kind"], f["object"], f["path"]) for f in findings] == [
        ("dimension", "visits.bad_band", "metadata.cube_sql"),
        ("dimension", "visits.broken_text", "metadata.cube_sql"),
        ("measure", "visits.escalating", "metadata.cube_sql"),
        ("measure_filter", "visits.escalating", "metadata.filters[1].sql"),
        ("measure", "visits.stale_ratio", "metadata.cube_sql"),
        ("relationship", "bad_join", "join_expression"),
    ]
    escalating = findings[2]
    assert escalating["object_id"] == str(bad_measure.id)
    assert escalating["workspace_id"] == str(model.workspace_id)
    assert escalating["visible"] is False
    assert escalating["impact"] == "not published"
    assert findings[4]["impact"] == "workspace Cube schema build fails"
    assert findings[5]["impact"] == "join dropped; schema still publishes"
    assert "pg_advisory_lock" in escalating["error"]
    assert {
        field.id: (field.metadata, field.is_visible)
        for field in SemanticField.objects.filter(dataset=visits)
    } == before

    text = StringIO()
    call_command("audit_semantic_sql", "--workspace-id", str(model.workspace_id), stdout=text)
    assert "Summary: 6 stored SQL fragment(s) fail validation." in text.getvalue()
