"""Data model history: every canvas commit is a revision that can be undone."""

from types import SimpleNamespace

import pytest

from apps.agents.tools import canvas_tool
from apps.agents.tools.canvas_tool import create_canvas_tools, destructive_deletions
from apps.artifacts.models import Artifact
from apps.chat.models import Thread
from apps.semantic.canvas import (
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
    SemanticCanvas,
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

    refusal = undo_revision(workspace, first.id, user)["refused"]

    assert refusal["code"] == "CONFLICT"
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

    refusal = undo_revision(workspace, created.id, user)["refused"]

    assert refusal["code"] == "CONFLICT"
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


def _measure_op(name, **extra):
    return {
        "op": "create",
        "object_type": "field",
        "value": {"dataset": "raw_visits", "name": name, "field_type": "measure", **extra},
    }


def test_undo_refuses_to_remove_a_field_another_field_uses(canvas, semantic_model, workspace, user):
    _commit(canvas, user, [_measure_op("total_amount", measure_type="sum", expression="amount")])
    first = SemanticModelRevision.objects.get()
    _commit(
        canvas,
        user,
        [
            _measure_op(
                "avg_amount",
                measure_type="number",
                cube_sql="{total_amount}::numeric / NULLIF({total_amount}, 0)",
            )
        ],
    )

    refusal = undo_revision(workspace, first.id, user)["refused"]

    assert refusal["code"] == "CONFLICT"
    assert "raw_visits.avg_amount" in refusal["conflicts"][0]["message"]
    assert (
        semantic_model.datasets.get(name="raw_visits").fields.filter(name="total_amount").exists()
    )


def test_commit_refuses_to_rename_or_delete_a_field_another_field_uses(
    canvas, semantic_model, user
):
    _commit(canvas, user, [_measure_op("total_amount", measure_type="sum", expression="amount")])
    ratio_sql = "{total_amount}::numeric / NULLIF({total_amount}, 0)"
    _commit(canvas, user, [_measure_op("ratio", measure_type="number", cube_sql=ratio_sql)])

    for operation in (
        {"op": "set", "target": "field/raw_visits.total_amount/name", "value": "amount_total"},
        {"op": "delete_object", "object": "field/raw_visits.total_amount"},
    ):
        apply_operations(canvas, [operation], user)
        report = commit_canvas(canvas, user)
        assert report["blocked"] is True
        [problem] = report["blocking_diagnostics"]
        assert problem["code"] == "MEMBER_IN_USE"
        assert "raw_visits.ratio" in problem["message"]
        apply_operations(
            canvas, [{"op": "revert_object", "object": "field/raw_visits.total_amount"}], user
        )

    _commit(
        canvas,
        user,
        [
            {"op": "set", "target": "field/raw_visits.total_amount/name", "value": "amount_total"},
            {
                "op": "set",
                "target": "field/raw_visits.ratio/cube_sql",
                "value": ratio_sql.replace("total_amount", "amount_total"),
            },
        ],
    )
    assert (
        semantic_model.datasets.get(name="raw_visits").fields.filter(name="amount_total").exists()
    )


RENAME_TOTAL = {
    "op": "set",
    "target": "field/raw_visits.total_amount/name",
    "value": "amount_total",
}


def _blocking_message(canvas, user, operations):
    apply_operations(canvas, operations, user)
    [problem] = commit_canvas(canvas, user)["blocking_diagnostics"]
    assert problem["code"] == "MEMBER_IN_USE"
    return problem["message"]


def _total_and_user(canvas, user, cube_sql):
    _commit(canvas, user, [_measure_op("total_amount", measure_type="sum", expression="amount")])
    _commit(canvas, user, [_measure_op("ratio", measure_type="number", cube_sql=cube_sql)])


@pytest.mark.parametrize("cube_sql", ["{ total_amount } / 2", "{CUBE.total_amount} / 2"])
def test_commit_sees_every_reference_form_cube_accepts(canvas, semantic_model, user, cube_sql):
    _total_and_user(canvas, user, cube_sql)

    assert "raw_visits.ratio" in _blocking_message(canvas, user, [RENAME_TOTAL])


def test_commit_sees_a_join_drafted_in_the_same_batch(canvas, semantic_model, user):
    _commit(canvas, user, [_measure_op("total_amount", measure_type="sum", expression="amount")])
    join = {
        "op": "create",
        "object_type": "relationship",
        "value": {
            "from_dataset": "raw_visits",
            "from_field": "total_amount",
            "to_dataset": "raw_users",
            "to_field": "username",
            "relationship_type": "many_to_one",
        },
    }

    assert "Relationship" in _blocking_message(canvas, user, [join, RENAME_TOTAL])


def test_commit_ignores_references_cube_never_publishes(canvas, semantic_model, user):
    _total_and_user(canvas, user, "{total_amount} / 2")
    semantic_model.datasets.get(name="raw_visits").fields.filter(name="ratio").update(
        is_visible=False
    )

    _commit(canvas, user, [RENAME_TOTAL])


def test_commit_refuses_to_delete_a_dataset_named_bare(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    _commit(
        canvas,
        user,
        [_measure_op("stat_rows", measure_type="number", cube_sql="(select 1 from {visit_stats})")],
    )

    message = _blocking_message(
        canvas, user, [{"op": "delete_object", "object": "dataset/visit_stats"}]
    )
    assert "raw_visits.stat_rows" in message


def test_undo_sees_a_cube_prefixed_reference(canvas, semantic_model, workspace, user):
    _commit(canvas, user, [_measure_op("total_amount", measure_type="sum", expression="amount")])
    created = SemanticModelRevision.objects.get()
    _commit(
        canvas,
        user,
        [_measure_op("ratio", measure_type="number", cube_sql="{CUBE.total_amount} / 2")],
    )

    refusal = undo_revision(workspace, created.id, user)["refused"]

    assert "raw_visits.ratio" in refusal["conflicts"][0]["message"]


def test_undo_refuses_to_restore_sql_that_fails_todays_field_rules(
    canvas, semantic_model, workspace, user
):
    paid = "CASE WHEN {CUBE}.\"amount\" > 0 THEN 'Paid' ELSE 'Unpaid' END"
    _commit(
        canvas,
        user,
        [
            {
                "op": "create",
                "object_type": "field",
                "value": {
                    "dataset": "raw_visits",
                    "name": "paid_status",
                    "field_type": "dimension",
                    "data_type": "text",
                    "cube_sql": paid,
                },
            }
        ],
    )
    _commit(
        canvas,
        user,
        [
            {
                "op": "set",
                "target": "field/raw_visits.paid_status/cube_sql",
                "value": paid.replace("> 0", "> 10"),
            }
        ],
    )
    edit = SemanticModelRevision.objects.order_by("-created_at").first()
    # Stands in for SQL that was valid when saved but fails rules added since.
    [entry] = edit.changes
    entry["before"]["metadata"]["cube_sql"] = '{CUBE}."no_such_column"'
    edit.save(update_fields=["changes"])

    refusal = undo_revision(workspace, edit.id, user)["refused"]

    assert refusal["code"] == "INVALID"
    assert refusal["conflicts"][0]["object"] == "field/raw_visits.paid_status"
    field = semantic_model.datasets.get(name="raw_visits").fields.get(name="paid_status")
    assert "> 10" in field.metadata["cube_sql"]
    assert not SemanticModelRevision.objects.filter(reverts=edit).exists()


def test_undo_refuses_to_restore_a_dataset_whose_sql_no_longer_compiles(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    _commit(canvas, user, [{"op": "delete_object", "object": "dataset/visit_stats"}])
    deleted = SemanticModelRevision.objects.order_by("-created_at").first()
    SemanticDataset.objects.filter(name="raw_visits").update(is_visible=False)

    refusal = undo_revision(workspace, deleted.id, user)["refused"]

    assert "SQL no longer works" in refusal["conflicts"][0]["message"]
    assert not SemanticDataset.objects.filter(name="visit_stats").exists()


def test_undo_of_a_field_and_its_dataset_deleted_together_restores_both(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    _commit(
        canvas,
        user,
        [
            {
                "op": "create",
                "object_type": "field",
                "value": {
                    "dataset": "visit_stats",
                    "name": "total_visits",
                    "field_type": "measure",
                    "measure_type": "sum",
                    "expression": "visit_count",
                },
            }
        ],
    )
    # A fresh thread stages the field changes before the dataset delete, so they commit first.
    fresh = resolve_thread_canvas(
        workspace, Thread.objects.create(workspace=workspace, user=user), user
    )
    _commit(
        fresh,
        user,
        [
            {"op": "set", "target": "field/visit_stats.visit_count/label", "value": "Visits"},
            {"op": "delete_object", "object": "field/visit_stats.total_visits"},
            {"op": "delete_object", "object": "dataset/visit_stats"},
        ],
    )
    deleted = SemanticModelRevision.objects.order_by("-created_at").first()
    assert {entry["object_type"] for entry in deleted.changes} == {"field", "dataset"}

    result = undo_revision(workspace, deleted.id, user)

    assert "refused" not in result, result
    restored = semantic_model.datasets.get(name="visit_stats")
    assert restored.fields.filter(name="total_visits").exists()
    assert restored.fields.get(name="visit_count").label != "Visits"


def test_undo_of_a_dataset_create_refuses_while_another_dataset_references_it(
    canvas, semantic_model, workspace, user, custom_sql
):
    _create_visit_stats(canvas, user)
    created = SemanticModelRevision.objects.get()
    _commit(
        canvas,
        user,
        [_measure_op("stat_visits", measure_type="number", cube_sql="{visit_stats.visit_count}")],
    )

    refusal = undo_revision(workspace, created.id, user)["refused"]

    assert refusal["code"] == "CONFLICT"
    assert "raw_visits.stat_visits" in refusal["conflicts"][0]["message"]
    assert semantic_model.datasets.filter(name="visit_stats").exists()


def test_undo_twice_is_refused(canvas, semantic_model, workspace, user):
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    revision = SemanticModelRevision.objects.get()
    undo_revision(workspace, revision.id, user)

    refusal = undo_revision(workspace, revision.id, user)["refused"]

    assert refusal["code"] == "ALREADY_UNDONE"


def test_undo_is_scoped_to_the_workspace(canvas, semantic_model, user):
    other_workspace = Workspace.objects.create(name="Other", created_by=user)
    _commit(canvas, user, [{"op": "set", "target": "dataset/raw_visits/label", "value": "One"}])
    revision = SemanticModelRevision.objects.get()

    refusal = undo_revision(other_workspace, revision.id, user)["refused"]

    assert refusal["code"] == "NOT_FOUND"


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


def _tools(workspace, user, thread, human_turn=1):
    return {
        t.name: t
        for t in create_canvas_tools(workspace, user, str(thread.id), human_turn=human_turn)
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_writer_agent_commit_is_versioned_and_undo_restores_it(
    workspace, user, semantic_model
):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tools = _tools(workspace, user, thread)

    await tools["canvas_apply"].ainvoke(
        {"operations": [{"op": "set", "target": "dataset/raw_visits/label", "value": "Site"}]}
    )
    report = await tools["canvas_commit"].ainvoke({})

    assert report["committed"][0]["name"] == "raw_visits"
    revision_id = report["revision"]["id"]
    history = await tools["canvas_history"].ainvoke({})
    assert history["revisions"][0]["id"] == revision_id
    assert (await SemanticDataset.objects.aget(name="raw_visits")).label == "Site"

    undone = await tools["canvas_undo"].ainvoke({"revision_id": revision_id})

    assert undone["undone"]["id"] == revision_id
    assert (await SemanticDataset.objects.aget(name="raw_visits")).label == "Visits"
    assert (await SemanticModelRevision.objects.aget(reverts_id=revision_id)).thread_id == (
        thread.id
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_only_member_cannot_undo_through_the_agent(
    workspace, user, read_user, semantic_model
):
    writer_thread = await Thread.objects.acreate(workspace=workspace, user=user)
    writer_tools = _tools(workspace, user, writer_thread)
    await writer_tools["canvas_apply"].ainvoke(
        {"operations": [{"op": "set", "target": "dataset/raw_visits/label", "value": "Site"}]}
    )
    revision_id = (await writer_tools["canvas_commit"].ainvoke({}))["revision"]["id"]
    reader_thread = await Thread.objects.acreate(workspace=workspace, user=read_user)
    reader_tools = _tools(workspace, read_user, reader_thread)

    result = await reader_tools["canvas_undo"].ainvoke({"revision_id": revision_id})

    assert result["errors"][0]["code"] == "FORBIDDEN"
    assert (await SemanticDataset.objects.aget(name="raw_visits")).label == "Site"
    history = await reader_tools["canvas_history"].ainvoke({})
    assert history["revisions"][0]["id"] == revision_id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_agent_delete_of_a_dataset_an_artifact_uses_waits_for_confirmation(
    workspace, user, semantic_model, custom_sql
):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tools = _tools(workspace, user, thread)
    await tools["canvas_apply"].ainvoke(
        {
            "operations": [
                {
                    "op": "create",
                    "object_type": "custom_dataset",
                    "value": {
                        "name": "visit_stats",
                        "primary_key": "username",
                        "definition_sql": "select username from raw_visits",
                    },
                }
            ]
        }
    )
    assert (await tools["canvas_commit"].ainvoke({}))["committed"]
    await Artifact.objects.acreate(
        workspace=workspace,
        title="Weekly visits",
        code="",
        conversation_id=str(thread.id),
        semantic_queries=[{"measures": ["visit_stats.count"], "dimensions": []}],
    )
    await tools["canvas_apply"].ainvoke(
        {"operations": [{"op": "delete_object", "object": "dataset/visit_stats"}]}
    )

    asked = await tools["canvas_commit"].ainvoke({})

    assert asked["committed"] == []
    assert asked["blocked"] is True
    assert asked["confirmation_required"] == [
        {"object": "dataset/visit_stats", "used_by_artifacts": ["Weekly visits"]}
    ]
    assert await SemanticDataset.objects.filter(name="visit_stats").aexists()

    # The agent cannot confirm in the turn it was told to ask.
    self_confirmed = await tools["canvas_commit"].ainvoke(
        {"confirmed_deletions": ["dataset/visit_stats"]}
    )
    assert self_confirmed["confirmation_required"]
    assert await SemanticDataset.objects.filter(name="visit_stats").aexists()

    next_turn = _tools(workspace, user, thread, human_turn=2)
    confirmed = await next_turn["canvas_commit"].ainvoke(
        {"confirmed_deletions": ["dataset/visit_stats"]}
    )

    assert confirmed["committed"][0]["change_type"] == "delete"
    assert not await SemanticDataset.objects.filter(name="visit_stats").aexists()
    assert confirmed["revision"]["summary"] == "Deleted dataset visit_stats"

    # Undoing the delete re-creates it; undoing that re-creation is a delete again.
    restored = await next_turn["canvas_undo"].ainvoke({"revision_id": confirmed["revision"]["id"]})
    asked_again = await next_turn["canvas_undo"].ainvoke(
        {"revision_id": restored["revision"]["id"], "confirmed_deletions": ["dataset/visit_stats"]}
    )

    assert asked_again["confirmation_required"][0]["object"] == "dataset/visit_stats"
    assert await SemanticDataset.objects.filter(name="visit_stats").aexists()

    await _tools(workspace, user, thread, human_turn=3)["canvas_undo"].ainvoke(
        {
            "revision_id": restored["revision"]["id"],
            "confirmed_deletions": ["dataset/visit_stats"],
        }
    )
    assert not await SemanticDataset.objects.filter(name="visit_stats").aexists()


@pytest.mark.django_db
def test_deleting_a_field_no_artifact_uses_needs_no_confirmation(canvas, semantic_model, user):
    _commit(
        canvas,
        user,
        [
            {
                "op": "create",
                "object_type": "field",
                "value": {
                    "dataset": "raw_visits",
                    "name": "total_amount",
                    "field_type": "measure",
                    "measure_type": "sum",
                    "expression": "amount",
                },
            }
        ],
    )
    Artifact.objects.create(
        workspace=canvas.workspace,
        title="Amounts",
        code="",
        conversation_id="c",
        semantic_queries=[{"measures": ["raw_visits.total_amount_x"]}],
    )
    apply_operations(
        canvas, [{"op": "delete_object", "object": "field/raw_visits.total_amount"}], user
    )

    assert destructive_deletions(canvas) == []

    Artifact.objects.create(
        workspace=canvas.workspace,
        title="Totals",
        code="",
        conversation_id="c",
        semantic_queries=[{"measures": ["raw_visits.total_amount"]}],
    )
    assert destructive_deletions(canvas) == [
        {"object": "field/raw_visits.total_amount", "used_by_artifacts": ["Totals"]}
    ]

    old_story = Artifact.objects.create(
        workspace=canvas.workspace,
        title="Old story",
        code="",
        conversation_id="c",
        data={"story_doc": {"blocks": [{"query": {"measures": ["raw_visits.total_amount"]}}]}},
    )
    assert sorted(destructive_deletions(canvas)[0]["used_by_artifacts"]) == ["Old story", "Totals"]

    Artifact.objects.create(
        workspace=canvas.workspace,
        title="Old story v2",
        code="",
        conversation_id="c",
        parent_artifact=old_story,
    )
    assert destructive_deletions(canvas)[0]["used_by_artifacts"] == ["Totals"]

    Artifact.objects.create(
        workspace=canvas.workspace,
        title="Totals v2 (failed write)",
        code="",
        conversation_id="c",
        parent_artifact=Artifact.objects.get(title="Totals"),
        is_deleted=True,
    )
    assert destructive_deletions(canvas)[0]["used_by_artifacts"] == ["Totals"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_confirmation_survives_a_commit_that_writes_nothing(
    workspace, user, semantic_model, custom_sql, monkeypatch
):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tools = _tools(workspace, user, thread)
    await tools["canvas_apply"].ainvoke(
        {
            "operations": [
                {
                    "op": "create",
                    "object_type": "custom_dataset",
                    "value": {
                        "name": "visit_stats",
                        "primary_key": "username",
                        "definition_sql": "select username from raw_visits",
                    },
                }
            ]
        }
    )
    assert (await tools["canvas_commit"].ainvoke({}))["committed"]
    await tools["canvas_apply"].ainvoke(
        {"operations": [{"op": "delete_object", "object": "dataset/visit_stats"}]}
    )
    assert (await tools["canvas_commit"].ainvoke({}))["confirmation_required"]

    real_commit = canvas_tool.commit_canvas
    blocked = {"committed": [], "blocked": True, "conflicts": [], "blocking_diagnostics": []}
    monkeypatch.setattr(canvas_tool, "commit_canvas", lambda *_args, **_kwargs: blocked)
    next_turn = _tools(workspace, user, thread, human_turn=2)
    confirmed = {"confirmed_deletions": ["dataset/visit_stats"]}
    assert (await next_turn["canvas_commit"].ainvoke(confirmed))["blocked"] is True

    monkeypatch.setattr(canvas_tool, "commit_canvas", real_commit)
    retried = await next_turn["canvas_commit"].ainvoke(confirmed)

    assert retried["committed"][0]["change_type"] == "delete"
    assert not await SemanticDataset.objects.filter(name="visit_stats").aexists()
    canvas_row = await SemanticCanvas.objects.aget(thread_id=thread.id)
    assert "dataset/visit_stats" not in canvas_row.pending_confirmations


async def _stage_visit_stats_delete(workspace, user, thread):
    tools = _tools(workspace, user, thread)
    await tools["canvas_apply"].ainvoke(
        {
            "operations": [
                {
                    "op": "create",
                    "object_type": "custom_dataset",
                    "value": {
                        "name": "visit_stats",
                        "primary_key": "username",
                        "definition_sql": "select username from raw_visits",
                    },
                }
            ]
        }
    )
    assert (await tools["canvas_commit"].ainvoke({}))["committed"]
    await tools["canvas_apply"].ainvoke(
        {"operations": [{"op": "delete_object", "object": "dataset/visit_stats"}]}
    )
    assert (await tools["canvas_commit"].ainvoke({}))["confirmation_required"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unconfirmed_retry_does_not_void_the_users_answer(
    workspace, user, semantic_model, custom_sql
):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await _stage_visit_stats_delete(workspace, user, thread)
    next_turn = _tools(workspace, user, thread, human_turn=2)

    assert (await next_turn["canvas_commit"].ainvoke({}))["confirmation_required"]
    confirmed = await next_turn["canvas_commit"].ainvoke(
        {"confirmed_deletions": ["dataset/visit_stats"]}
    )

    assert confirmed["committed"][0]["change_type"] == "delete"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_confirmation_counts_a_few_turns_later_but_not_long_after(
    workspace, user, semantic_model, custom_sql
):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    await _stage_visit_stats_delete(workspace, user, thread)
    confirmed = {"confirmed_deletions": ["dataset/visit_stats"]}

    stale = await _tools(workspace, user, thread, human_turn=5)["canvas_commit"].ainvoke(confirmed)
    assert stale["confirmation_required"]

    later = await _tools(workspace, user, thread, human_turn=7)["canvas_commit"].ainvoke(confirmed)
    assert later["committed"][0]["change_type"] == "delete"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_renaming_a_used_field_waits_for_confirmation_and_redefining_it_is_reported(
    workspace, user, semantic_model
):
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    tools = _tools(workspace, user, thread)
    await tools["canvas_apply"].ainvoke(
        {"operations": [_measure_op("total_amount", measure_type="sum", expression="amount")]}
    )
    assert (await tools["canvas_commit"].ainvoke({}))["committed"]
    await Artifact.objects.acreate(
        workspace=workspace,
        title="Totals",
        code="",
        conversation_id=str(thread.id),
        semantic_queries=[{"measures": ["raw_visits.total_amount"]}],
    )
    field = "field/raw_visits.total_amount"

    await tools["canvas_apply"].ainvoke(
        {"operations": [{"op": "set", "target": f"{field}/measure_type", "value": "max"}]}
    )
    redefined = await tools["canvas_commit"].ainvoke({})

    assert redefined["committed"]
    assert redefined["redefined_fields_used_by_artifacts"] == [
        {"object": field, "used_by_artifacts": ["Totals"], "change": "redefine"}
    ]

    await tools["canvas_apply"].ainvoke(
        {
            "operations": [
                {"op": "set", "target": f"{field}/name", "value": "amount_total"},
                {"op": "set", "target": f"{field}/measure_type", "value": "min"},
            ]
        }
    )
    asked = await tools["canvas_commit"].ainvoke({})

    assert asked["confirmation_required"][0]["change"] == "rename"
    assert asked["blocking_diagnostics"][0]["message"].startswith(f"Renaming {field}")
    assert await SemanticField.objects.filter(name="total_amount").aexists()

    confirmed = await _tools(workspace, user, thread, human_turn=2)["canvas_commit"].ainvoke(
        {"confirmed_deletions": [field]}
    )
    assert confirmed["committed"]
    assert confirmed["redefined_fields_used_by_artifacts"][0]["change"] == "redefine"
    assert await SemanticField.objects.filter(name="amount_total").aexists()
