"""Data model history: every canvas commit is a revision that can be undone."""

from types import SimpleNamespace

import pytest

from apps.chat.models import Thread
from apps.semantic.canvas import (
    RevisionUndoError,
    apply_operations,
    commit_canvas,
    list_revisions,
    resolve_thread_canvas,
    undo_revision,
)
from apps.semantic.canvas import commit as canvas_commit_module
from apps.semantic.canvas import service as canvas_service
from apps.semantic.models import (
    CustomDataset,
    SemanticCanvasChange,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticModelRevision,
    SemanticRelationship,
)
from apps.workspaces.models import Workspace

CUSTOM_COLUMNS = [
    {"name": "username", "type": "text"},
    {"name": "visit_count", "type": "bigint"},
]


@pytest.fixture
def semantic_model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Model")
    visits = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="raw_visits",
        label="Visits",
        table_name="raw_visits",
        schema_name="tenant_schema",
        primary_key="visit_id",
    )
    users = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="raw_users",
        label="Users",
        table_name="raw_users",
        schema_name="tenant_schema",
        primary_key="username",
    )
    for dataset, column, data_type in (
        (visits, "visit_id", "bigint"),
        (visits, "username", "text"),
        (visits, "amount", "numeric"),
        (users, "username", "text"),
    ):
        SemanticField.objects.create(
            dataset=dataset,
            name=column,
            field_type=SemanticField.FieldType.DIMENSION,
            data_type=data_type,
            expression=column,
            metadata={"source_column": column},
        )
    return model


@pytest.fixture
def canvas(workspace, user, semantic_model):
    thread = Thread.objects.create(workspace=workspace, user=user)
    return resolve_thread_canvas(workspace, thread, user)


@pytest.fixture(autouse=True)
def cube_builds(monkeypatch):
    calls = []
    monkeypatch.setattr(
        canvas_commit_module,
        "build_and_promote_cube_schema",
        lambda workspace, model, **_: calls.append(model.id) or SimpleNamespace(content_hash="h"),
    )
    return calls


@pytest.fixture
def custom_sql(monkeypatch):
    monkeypatch.setattr(
        canvas_service, "infer_custom_dataset_columns", lambda _workspace, _sql: CUSTOM_COLUMNS
    )


def _commit(canvas, user, operations):
    applied = apply_operations(canvas, operations, user)
    assert "errors" not in applied, applied
    report = commit_canvas(canvas, user)
    assert report["committed"], report
    return report


def _create_visit_stats(canvas, user):
    return _commit(
        canvas,
        user,
        [
            {
                "op": "create",
                "object_type": "custom_dataset",
                "value": {
                    "name": "visit_stats",
                    "primary_key": "username",
                    "definition_sql": (
                        "select username, count(*) as visit_count from raw_visits group by username"
                    ),
                },
            }
        ],
    )


def test_commit_records_a_revision_with_before_and_after(canvas, semantic_model, user):
    report = _commit(
        canvas,
        user,
        [{"op": "set", "target": "dataset/raw_visits/label", "value": "Site visits"}],
    )

    revision = SemanticModelRevision.objects.get()
    assert report["revision"] == {"id": str(revision.id), "summary": revision.summary}
    assert revision.summary == "Edited dataset raw_visits"
    assert revision.created_by == user
    assert revision.thread_id == canvas.thread_id
    [entry] = revision.changes
    assert entry["before"]["label"] == "Visits"
    assert entry["after"]["label"] == "Site visits"


def test_undo_restores_the_previous_label_and_curation(
    canvas, semantic_model, workspace, user, cube_builds
):
    _commit(
        canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "Site visits"}]
    )
    revision = SemanticModelRevision.objects.get()

    result = undo_revision(workspace, revision.id, user)

    visits = semantic_model.datasets.get(name="raw_visits")
    assert visits.label == "Visits"
    assert "label" not in visits.metadata.get("curated_fields", [])
    assert result["undone"]["id"] == str(revision.id)
    assert result["revision"]["summary"] == "Undid: Edited dataset raw_visits"
    assert cube_builds == [semantic_model.id, semantic_model.id]
    history = list_revisions(workspace)
    assert [item["undone"] for item in history] == [False, True]


def test_undo_of_a_created_custom_dataset_removes_it_and_redo_brings_it_back(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    created = semantic_model.datasets.get(name="visit_stats")
    field_ids = set(created.fields.values_list("id", flat=True))
    revision = SemanticModelRevision.objects.get()

    undo = undo_revision(workspace, revision.id, user)

    assert not SemanticDataset.objects.filter(name="visit_stats").exists()
    assert not CustomDataset.objects.filter(name="visit_stats").exists()
    assert not SemanticCanvasChange.objects.filter(object_uuid=created.id).exists()

    undo_revision(workspace, undo["revision"]["id"], user)

    restored = semantic_model.datasets.get(name="visit_stats")
    assert restored.id == created.id
    assert restored.custom_dataset.definition_sql.startswith("select username")
    assert set(restored.fields.values_list("id", flat=True)) == field_ids


def test_undo_of_a_deleted_custom_dataset_restores_its_fields_and_joins(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    _commit(
        canvas,
        user,
        [
            {
                "op": "create",
                "object_type": "relationship",
                "value": {
                    "from_dataset": "raw_visits",
                    "from_field": "username",
                    "to_dataset": "visit_stats",
                    "to_field": "username",
                    "relationship_type": "many_to_one",
                },
            }
        ],
    )
    _commit(canvas, user, [{"op": "delete_object", "object": "dataset/visit_stats"}])
    assert not SemanticRelationship.objects.exists()
    delete_revision = SemanticModelRevision.objects.order_by("-created_at").first()

    undo_revision(workspace, delete_revision.id, user)

    restored = semantic_model.datasets.get(name="visit_stats")
    assert restored.custom_dataset is not None
    assert restored.fields.filter(name="visit_count").exists()
    assert SemanticRelationship.objects.get().to_dataset_id == restored.id


def test_undo_refuses_to_overwrite_a_later_edit(canvas, semantic_model, workspace, user):
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    first = SemanticModelRevision.objects.get()
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "Two"}])
    second = SemanticModelRevision.objects.exclude(id=first.id).get()

    with pytest.raises(RevisionUndoError) as exc_info:
        undo_revision(workspace, first.id, user)

    assert exc_info.value.code == "CONFLICT"
    assert semantic_model.datasets.get(name="raw_visits").label == "Two"
    assert SemanticModelRevision.objects.count() == 2

    undo_revision(workspace, second.id, user)
    undo_revision(workspace, first.id, user)
    assert semantic_model.datasets.get(name="raw_visits").label == "Visits"


def test_undo_of_a_create_refuses_after_a_later_field_edit(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    created = SemanticModelRevision.objects.get()
    _commit(
        canvas,
        user,
        [{"op": "set", "target": "field/visit_stats.visit_count/label", "value": "Visits"}],
    )

    with pytest.raises(RevisionUndoError) as exc_info:
        undo_revision(workspace, created.id, user)

    assert exc_info.value.code == "CONFLICT"
    assert semantic_model.datasets.filter(name="visit_stats").exists()


def test_undo_of_a_create_ignores_catalog_refresh_drift(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    revision = SemanticModelRevision.objects.get()
    # A refresh re-derives uncurated labels and generated field metadata.
    SemanticDataset.objects.filter(name="visit_stats").update(label="Visit Stats")
    SemanticField.objects.filter(dataset__name="visit_stats").update(
        metadata={"source_column": "x", "nullable": True}
    )

    undo_revision(workspace, revision.id, user)

    assert not semantic_model.datasets.filter(name="visit_stats").exists()


def test_undo_twice_is_refused(canvas, semantic_model, workspace, user):
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    revision = SemanticModelRevision.objects.get()
    undo_revision(workspace, revision.id, user)

    with pytest.raises(RevisionUndoError) as exc_info:
        undo_revision(workspace, revision.id, user)

    assert exc_info.value.code == "ALREADY_UNDONE"


def test_undo_is_scoped_to_the_workspace(canvas, semantic_model, user):
    other_workspace = Workspace.objects.create(name="Other", created_by=user)
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    revision = SemanticModelRevision.objects.get()

    with pytest.raises(RevisionUndoError) as exc_info:
        undo_revision(other_workspace, revision.id, user)

    assert exc_info.value.code == "NOT_FOUND"


def test_revision_api_lists_and_undoes_for_writers(client, canvas, semantic_model, workspace, user):
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    revision = SemanticModelRevision.objects.get()
    client.force_login(user)
    base = f"/api/workspaces/{workspace.id}/data-model/revisions/"

    listing = client.get(base).json()
    assert listing["can_undo"] is True
    assert listing["revisions"][0]["summary"] == "Edited dataset raw_visits"

    response = client.post(f"{base}{revision.id}/undo/")
    assert response.status_code == 200
    assert semantic_model.datasets.get(name="raw_visits").label == "Visits"
    assert client.post(f"{base}{revision.id}/undo/").status_code == 409


def test_revision_api_refuses_undo_for_read_only_members(
    client, canvas, semantic_model, workspace, user, read_user
):
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    revision = SemanticModelRevision.objects.get()
    client.force_login(read_user)
    base = f"/api/workspaces/{workspace.id}/data-model/revisions/"

    assert client.get(base).json()["can_undo"] is False
    assert client.post(f"{base}{revision.id}/undo/").status_code == 403
    assert semantic_model.datasets.get(name="raw_visits").label == "One"
