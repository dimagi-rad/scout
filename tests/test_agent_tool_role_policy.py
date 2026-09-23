"""Role enforcement for agent-local tools and graph capabilities."""

from unittest.mock import AsyncMock, Mock

import pytest

from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.agents.tools.canvas_tool import create_canvas_tools
from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.agents.tools.materialization_tool import create_materialization_tool
from apps.agents.tools.recipe_tool import create_recipe_tool
from apps.artifacts.models import Artifact
from apps.knowledge.models import AgentLearning
from apps.recipes.models import Recipe
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole


def _by_name(tools):
    return {item.name: item for item in tools}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_user_cannot_directly_invoke_local_mutation_sinks(
    workspace, read_user, monkeypatch
):
    materialize = AsyncMock(return_value={"all_succeeded": True, "tenants": []})
    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", materialize)

    artifact_write = _by_name(create_artifact_graph_tools(workspace, read_user))["artifact_write"]
    create_artifact = _by_name(create_artifact_tools(workspace, read_user))["create_artifact"]
    save_learning = create_save_learning_tool(workspace, read_user)
    save_recipe = create_recipe_tool(workspace, read_user)
    blocking_materialization = create_materialization_tool(workspace, read_user)

    results = [
        await artifact_write.ainvoke(
            {"action": "create", "title": "Denied", "story_doc": {"schema_version": 1}}
        ),
        await create_artifact.ainvoke(
            {"title": "Denied", "artifact_type": "markdown", "code": "# denied"}
        ),
        await save_learning.ainvoke(
            {
                "description": "This detailed learning must not be saved by a reader.",
                "category": "other",
                "tables": ["cases"],
            }
        ),
        await save_recipe.ainvoke(
            {
                "name": "Denied",
                "description": "",
                "variables": [],
                "prompt": "Analyze cases",
            }
        ),
        await blocking_materialization.ainvoke({}),
    ]

    assert all(result["status"] == "denied" for result in results)
    assert all(result["error"]["code"] == "FORBIDDEN" for result in results)
    assert await Artifact.objects.filter(workspace=workspace).acount() == 0
    assert await AgentLearning.objects.filter(workspace=workspace).acount() == 0
    assert await Recipe.objects.filter(workspace=workspace).acount() == 0
    materialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tools_recheck_role_after_graph_construction(workspace, write_user):
    tool = create_save_learning_tool(workspace, write_user)
    await WorkspaceMembership.objects.filter(workspace=workspace, user=write_user).aupdate(
        role=WorkspaceRole.READ
    )

    result = await tool.ainvoke(
        {
            "description": "A role downgrade after graph creation must deny this save.",
            "category": "other",
            "tables": ["cases"],
        }
    )

    assert result["status"] == "denied"
    assert await AgentLearning.objects.filter(workspace=workspace).acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tools_recheck_membership_removal_after_graph_construction(workspace, write_user):
    tool = create_recipe_tool(workspace, write_user)
    await WorkspaceMembership.objects.filter(workspace=workspace, user=write_user).adelete()

    result = await tool.ainvoke(
        {"name": "Denied", "description": "", "variables": [], "prompt": "Analyze cases"}
    )

    assert result["status"] == "denied"
    assert await Recipe.objects.filter(workspace=workspace).acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_manage_user_can_invoke_local_mutation(workspace, user):
    result = await create_recipe_tool(workspace, user).ainvoke(
        {"name": "Allowed", "description": "", "variables": [], "prompt": "Analyze cases"}
    )

    assert result["status"] == "created"
    assert await Recipe.objects.filter(workspace=workspace, name="Allowed").aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_write_user_can_invoke_local_mutation(workspace, write_user):
    result = await create_recipe_tool(workspace, write_user).ainvoke(
        {"name": "Writer allowed", "description": "", "variables": [], "prompt": "Analyze"}
    )

    assert result["status"] == "created"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_user_cannot_invoke_legacy_update_sink(workspace, read_user, user):
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Existing",
        artifact_type="markdown",
        code="# original",
    )
    update_artifact = _by_name(create_artifact_tools(workspace, read_user))["update_artifact"]

    result = await update_artifact.ainvoke({"artifact_id": str(artifact.id), "code": "# changed"})

    assert result["status"] == "denied"
    await artifact.arefresh_from_db()
    assert artifact.code == "# original"
    assert await Artifact.objects.filter(workspace=workspace).acount() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_absent_actor_cannot_invoke_local_mutation(workspace):
    result = await create_recipe_tool(workspace, None).ainvoke(
        {"name": "Denied", "description": "", "variables": [], "prompt": "Analyze"}
    )

    assert result["status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_user_can_inspect_artifact(workspace, read_user, user):
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Readable",
        artifact_type="story",
        code="",
        data={"story_doc": {"schema_version": 1, "name": "Readable", "blocks": []}},
    )
    overview = _by_name(create_artifact_graph_tools(workspace, read_user))[
        "artifact_graph_overview"
    ]

    result = await overview.ainvoke({"artifact_id": str(artifact.id)})

    assert result["status"] == "ok"
    assert result["artifact"]["id"] == str(artifact.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_artifact_inspection_rechecks_removed_membership(workspace, read_user, user):
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=user,
        title="Revoked",
        artifact_type="story",
        code="",
        data={"story_doc": {"schema_version": 1, "name": "Revoked", "blocks": []}},
    )
    overview = _by_name(create_artifact_graph_tools(workspace, read_user))[
        "artifact_graph_overview"
    ]
    await WorkspaceMembership.objects.filter(workspace=workspace, user=read_user).adelete()

    result = await overview.ainvoke({"artifact_id": str(artifact.id)})

    assert result["status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("actor_state", ["absent", "revoked", "read"])
async def test_canvas_sampling_rechecks_read_access(workspace, read_user, monkeypatch, actor_state):
    sample = Mock(return_value={"rows": [{"count": 1}]})
    monkeypatch.setattr("apps.agents.tools.canvas_tool.sample_dataset_rows", sample)
    actor = None if actor_state == "absent" else read_user
    sample_tool = _by_name(create_canvas_tools(workspace, actor, "thread"))["canvas_sample_rows"]
    if actor_state == "revoked":
        await WorkspaceMembership.objects.filter(workspace=workspace, user=read_user).adelete()

    result = await sample_tool.ainvoke({"dataset": "cases"})

    if actor_state == "read":
        assert result == {"rows": [{"count": 1}]}
        sample.assert_called_once_with(workspace, "cases", 5, None)
    else:
        assert result["errors"][0]["code"] == "FORBIDDEN"
        sample.assert_not_called()
