"""Time filters remain dependencies without inventing Cube result columns."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.db import connections

from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.artifacts.models import Artifact, ArtifactSemanticQuery
from apps.artifacts.services.graph_doc import expected_result_keys, validate_doc
from apps.artifacts.services.graph_manifest import build_semantic_query_manifest
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.chat.models import Thread, ThreadArtifact
from apps.semantic.services.query import _cube_query
from apps.users.models import User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole


def topic_query(**overrides):
    return {
        "measures": ["topics.count"],
        "dimensions": ["topics.topic"],
        "time_dimension": "topics.created_at",
        **overrides,
    }


def topic_story(query):
    return {
        "schema_version": 1,
        "blocks": [
            {"id": "period", "type": "date_filter", "config": {"default": "last_30_days"}},
            {
                "id": "q",
                "type": "semantic_query",
                "hidden": True,
                "inputs": {"date_range": {"$ref": "period.value"}},
                "config": {"queries": {"topics": query}},
            },
            {
                "id": "chart",
                "type": "graph",
                "inputs": {"data": {"$ref": "q.topics"}},
                "config": {
                    "chart_type": "bar",
                    "x_key": "topics_topic",
                    "series": ["topics_count"],
                },
            },
            {
                "id": "table",
                "type": "table",
                "inputs": {"data": {"$ref": "q.topics"}},
                "config": {"columns": ["topics_topic", "topics_count"]},
            },
        ],
    }


QUERY_CASES = [
    pytest.param(topic_query(), None, id="filter-only"),
    pytest.param(topic_query(granularity=None), None, id="null-granularity"),
    pytest.param(topic_query(granularity="day"), "date", id="bucketed"),
    pytest.param(
        topic_query(dimensions=["topics.topic", "topics.created_at"]),
        "topics_created_at",
        id="explicit-time-dimension",
    ),
]


@pytest.mark.parametrize("query,time_key", QUERY_CASES)
def test_time_result_contract_preserves_dependencies(query, time_key):
    expected = {"topics_count", "topics_topic"} | ({time_key} if time_key else set())
    manifest = build_semantic_query_manifest(topic_story(query))
    assert validate_doc(topic_story(query)) == []
    assert expected_result_keys(query) == expected
    assert manifest["unresolved"] == []
    entry = manifest["entries"][0]
    assert set(entry["result_keys"]) == expected
    assert entry["query"]["time_dimension"] == "topics.created_at"
    assert entry["members"] == ["topics.count", "topics.created_at", "topics.topic"]
    assert {"kind": "semantic_member", "name": "topics.created_at"} in entry["dependencies"]


@pytest.mark.parametrize("block_index,config_key", [(2, "x_key"), (3, "columns")])
def test_filter_only_time_cannot_be_used_as_a_returned_column(block_index, config_key):
    story = topic_story(topic_query())
    story["blocks"][block_index]["config"][config_key] = (
        "topics_created_at" if config_key == "x_key" else ["topics_created_at", "topics_count"]
    )
    diagnostics = validate_doc(story)
    assert [item["code"] for item in diagnostics] == ["missing_result_key:topics_created_at"]


@pytest.mark.parametrize("granularity", ["", "day"])
def test_cube_filter_and_projection_are_distinct(granularity):
    count = SimpleNamespace(member="topics.count")
    topic = SimpleNamespace(member="topics.topic")
    time = SimpleNamespace(member="topics.created_at")
    result = _cube_query(
        resolved_measures=[count],
        resolved_dimensions=[topic],
        resolved_time=time,
        granularity=granularity,
        resolved_filters=[
            (time, {"operator": "inDateRange", "values": ["2026-09-01", "2026-09-15"]})
        ],
        order_by=[],
        limit=100,
    )
    assert result["dimensions"] == ["topics.topic"]
    assert result["timeDimensions"] == [
        {"dimension": "topics.created_at", **({"granularity": granularity} if granularity else {})}
    ]
    assert result["filters"] == [
        {
            "member": "topics.created_at",
            "operator": "inDateRange",
            "values": ["2026-09-01", "2026-09-15"],
        }
    ]


def cube_result(query, time_key):
    columns = ["topics.topic", "topics.count"]
    row = ["Account update", 2]
    if time_key:
        columns.append("topics.created_at.day" if query.get("granularity") else "topics.created_at")
        row.append("2026-09-12T00:00:00.000")
    return {"columns": columns, "rows": [row], "row_count": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("query,time_key", QUERY_CASES)
@pytest.mark.parametrize("row_format", ["array", "object"])
async def test_runtime_accepts_actual_cube_projection(query, time_key, row_format):
    result = cube_result(query, time_key)
    if row_format == "object":
        result["rows"] = [dict(zip(result["columns"], result["rows"][0], strict=True))]
    artifact = SimpleNamespace(workspace_id="workspace", data={"story_doc": topic_story(query)})
    workspace = SimpleNamespace(id="workspace")
    with (
        patch(
            "apps.artifacts.services.graph_runtime.Workspace.objects.aget",
            new=AsyncMock(return_value=workspace),
        ),
        patch(
            "apps.artifacts.services.graph_runtime.run_semantic_query",
            new=AsyncMock(return_value=result),
        ) as execute,
    ):
        runtime = await check_graph_artifact(artifact, user_id="synthetic-user")
    assert runtime["success"] is True
    assert runtime["key_warnings"] == []
    assert runtime["queries"][0]["result_keys"] == sorted(expected_result_keys(query))
    assert execute.await_args.args[0] is workspace
    assert execute.await_args.args[1]["time_dimension"] == "topics.created_at"
    assert execute.await_args.kwargs == {"user_id": "synthetic-user"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,time_key,missing",
    [
        (topic_query(), None, "topics.count"),
        (topic_query(), None, "topics.topic"),
        (topic_query(granularity="day"), "date", "topics.created_at.day"),
        (
            topic_query(dimensions=["topics.topic", "topics.created_at"]),
            "topics_created_at",
            "topics.created_at",
        ),
    ],
)
async def test_runtime_still_rejects_missing_selected_columns(query, time_key, missing):
    result = cube_result(query, time_key)
    index = result["columns"].index(missing)
    result["columns"].pop(index)
    result["rows"][0].pop(index)
    artifact = SimpleNamespace(workspace_id="workspace", data={"story_doc": topic_story(query)})
    with (
        patch("apps.artifacts.services.graph_runtime.Workspace.objects.aget", new=AsyncMock()),
        patch(
            "apps.artifacts.services.graph_runtime.run_semantic_query",
            new=AsyncMock(return_value=result),
        ),
    ):
        runtime = await check_graph_artifact(artifact)
    assert runtime["success"] is False
    assert [item["expected_key"] for item in runtime["key_warnings"]] == [
        "date" if missing.endswith(".day") else missing.replace(".", "_")
    ]


@pytest.mark.asyncio
async def test_filter_only_time_still_requires_a_valid_semantic_member():
    artifact = SimpleNamespace(
        workspace_id="workspace", data={"story_doc": topic_story(topic_query())}
    )
    error = {"success": False, "error": {"message": "Unknown semantic field 'topics.created_at'."}}
    with (
        patch("apps.artifacts.services.graph_runtime.Workspace.objects.aget", new=AsyncMock()),
        patch(
            "apps.artifacts.services.graph_runtime.run_semantic_query",
            new=AsyncMock(return_value=error),
        ),
    ):
        runtime = await check_graph_artifact(artifact)
    assert runtime["success"] is False
    assert runtime["queries"][0]["status"] == "error"
    assert runtime["queries"][0]["error"] == error["error"]["message"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("query,time_key", QUERY_CASES)
async def test_artifact_write_publishes_and_updates_valid_time_query(
    query, time_key, close_thread_sensitive_connections
):
    user = await User.objects.acreate(email="filter-contract@localhost.invalid")
    workspace = await Workspace.objects.acreate(
        name="Synthetic date-filter contract", created_by=user
    )
    await WorkspaceMembership.objects.acreate(
        workspace=workspace, user=user, role=WorkspaceRole.READ_WRITE
    )
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    write = next(
        tool
        for tool in create_artifact_graph_tools(workspace, user, str(thread.id))
        if tool.name == "artifact_write"
    )
    with patch(
        "apps.artifacts.services.graph_runtime.run_semantic_query",
        new=AsyncMock(return_value=cube_result(query, time_key)),
    ):
        created = await write.ainvoke(
            {"action": "create", "title": "Topics", "story_doc": topic_story(query)}
        )
        assert created["status"] == "created", created
        updated = await write.ainvoke(
            {
                "action": "apply",
                "artifact_id": created["artifact"]["id"],
                "ops": [{"op": "set", "target": "story/name", "value": "Filtered topics"}],
            }
        )
    assert updated["status"] == "updated", updated
    assert created["runtime"]["success"] and updated["runtime"]["success"]
    artifact = await Artifact.objects.aget(pk=updated["artifact"]["id"])
    assert artifact.data["story_doc"]["blocks"][1]["inputs"] == {
        "date_range": {"$ref": "period.value"}
    }
    dependency = await ArtifactSemanticQuery.objects.aget(artifact=artifact)
    assert "topics.created_at" in dependency.members
    assert dependency.query_payload["time_dimension"] == "topics.created_at"
    assert await ThreadArtifact.objects.filter(thread=thread, artifact=artifact).aexists()


@pytest_asyncio.fixture
async def close_thread_sensitive_connections():
    """Close async ORM's connection even when an artifact assertion fails."""
    try:
        yield
    finally:
        await sync_to_async(connections.close_all, thread_sensitive=True)()
