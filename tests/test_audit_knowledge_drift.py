import json
import uuid
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge
from apps.knowledge.services.drift_audit import audit_knowledge_drift
from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel
from apps.workspaces.models import Workspace


@pytest.fixture
def catalog(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Model", version=3)
    cases = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="cases",
        table_name="raw_cases",
        metadata={"source_table_name": "raw_cases"},
    )
    SemanticField.objects.create(
        dataset=cases,
        name="status",
        field_type=SemanticField.FieldType.DIMENSION,
        expression="status",
        metadata={"source_column": "status"},
    )
    SemanticField.objects.create(
        dataset=cases,
        name="old_status",
        field_type=SemanticField.FieldType.DIMENSION,
        expression="old_status",
        is_visible=False,
        metadata={"source_column": "old_status"},
    )
    SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="forms",
        table_name="raw_forms",
        is_visible=False,
    )
    # Shares a name with prose like `docs.md`, which must not read as a member.
    SemanticDataset.objects.create(
        semantic_model=model, workspace=workspace, name="docs", table_name="raw_docs"
    )
    return model


@pytest.fixture
def knowledge(workspace, catalog):
    return {
        "valid_entry": KnowledgeEntry.objects.create(
            workspace=workspace, title="Open cases", content="Filter `cases.status` to open."
        ),
        "drifted_entry": KnowledgeEntry.objects.create(
            workspace=workspace,
            title="Legacy status",
            content="Use `cases.old_status`, joined to `raw_forms`; see `docs.md`.",
        ),
        "valid_table": TableKnowledge.objects.create(
            workspace=workspace,
            table_name="raw_cases",
            description="Cases",
            column_notes={"status": "open or closed"},
        ),
        "drifted_learning": AgentLearning.objects.create(
            workspace=workspace,
            description="Filter `visits` and `raw_forms` by date",
            applies_to_tables=["visits", "raw_forms", ""],
        ),
    }


def _audit(*args, stderr=None) -> str:
    out = StringIO()
    call_command("audit_knowledge_drift", *args, stdout=out, stderr=stderr or StringIO())
    return out.getvalue()


@pytest.mark.django_db
def test_json_reports_only_drifted_rows(workspace, knowledge):
    [report] = json.loads(_audit("--json"))

    assert report["workspace_id"] == str(workspace.id)
    assert report["catalog_status"] == "active"
    assert report["catalog_version"] == 3
    assert report["checked"] == 4
    drifted = {row["id"]: row["references"] for row in report["drifted"]}
    assert drifted == {
        str(knowledge["drifted_entry"].id): [
            {"kind": "member", "name": "cases.old_status", "reason": "hidden"},
            {"kind": "table", "name": "raw_forms", "reason": "hidden"},
        ],
        str(knowledge["drifted_learning"].id): [
            {"kind": "table", "name": "visits", "reason": "missing"},
            {"kind": "table", "name": "raw_forms", "reason": "hidden"},
        ],
    }


@pytest.mark.django_db
def test_table_knowledge_columns_related_tables_and_rendered_prose(workspace, catalog):
    cases = SemanticDataset.objects.get(workspace=workspace, name="cases")
    for name in ("legacy_a", "legacy_b"):
        SemanticField.objects.create(
            dataset=cases, name=name, field_type="dimension", expression=name, is_visible=False
        )
    table = TableKnowledge.objects.create(
        workspace=workspace,
        table_name="raw_cases",
        description="Cases; prefer `cases.old_status`.",
        data_quality_notes=["Join `raw_forms` late", 3],
        use_cases=["`forms` is hidden but never rendered"],
        column_notes={"status": "was `cases.legacy_a`", "old_status": "gone", "never": "typo"},
        related_tables=[
            {"table": "raw_forms", "join_hint": "on `cases.legacy_b`"},
            "raw_cases",
            "raw_forms",
        ],
    )

    [report] = json.loads(_audit("--json"))

    assert report["drifted"] == [
        {
            "source": "table_knowledge",
            "id": str(table.id),
            "label": "raw_cases",
            "references": [
                {"kind": "column", "name": "raw_cases.never", "reason": "missing"},
                {"kind": "column", "name": "raw_cases.old_status", "reason": "hidden"},
                {"kind": "table", "name": "raw_forms", "reason": "hidden"},
                {"kind": "member", "name": "cases.old_status", "reason": "hidden"},
                {"kind": "member", "name": "cases.legacy_a", "reason": "hidden"},
                {"kind": "member", "name": "cases.legacy_b", "reason": "hidden"},
            ],
        }
    ]


@pytest.mark.django_db
def test_text_output_and_no_writes(workspace, knowledge, user):
    no_catalog = Workspace.objects.create(name="Zeta no catalog", created_by=user)
    KnowledgeEntry.objects.create(workspace=no_catalog, title="Note", content="`cases.status`")
    Workspace.objects.create(name="Empty", created_by=user)
    clean = Workspace.objects.create(name="Clean", created_by=user)
    SemanticModel.objects.create(workspace=clean, name="Model")
    KnowledgeEntry.objects.create(workspace=clean, title="Prose", content="No references.")

    with CaptureQueriesContext(connection) as queries:
        output = _audit()

    writes = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes == []
    assert f"[DRIFT] workspace={workspace.name!r}" in output
    assert "2/4 rows reference drifted names" in output
    assert "member `cases.old_status` (hidden)" in output
    assert "Open cases" not in output
    assert "[SKIPPED] workspace='Zeta no catalog'" in output
    assert "Empty" not in output
    assert "[OK] workspace='Clean'" in output
    assert "0/1 rows reference drifted names" in output
    assert "Summary: 2 drifted rows across 2 audited workspaces; 1 skipped" in output


@pytest.mark.django_db
def test_workspace_filter_reports_requested_workspaces_and_unknown_ids(workspace, knowledge, user):
    other = Workspace.objects.create(name="Other", created_by=user)
    unknown = uuid.uuid4()
    err = StringIO()

    reports = json.loads(
        _audit(
            "--json", "--workspace-id", str(other.id), "--workspace-id", str(unknown), stderr=err
        )
    )

    assert [report["workspace_id"] for report in reports] == [str(other.id)]
    assert reports[0]["catalog_status"] == "unavailable"
    assert f"No workspace with id {unknown}." in err.getvalue()


@pytest.mark.django_db
def test_empty_workspace_id_list_audits_nothing(knowledge):
    assert audit_knowledge_drift(workspace_ids=[]) == []
