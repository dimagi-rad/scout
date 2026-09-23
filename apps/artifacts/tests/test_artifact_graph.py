from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient

from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.artifacts.models import Artifact, ArtifactSemanticQuery, ArtifactType
from apps.artifacts.services.graph_doc import GraphDocError, apply_ops, validate_doc
from apps.artifacts.services.graph_manifest import (
    semantic_query_summary,
    sync_artifact_semantic_query_manifest,
)
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.chat.models import Thread, ThreadArtifact
from apps.users.models import Tenant, User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from tests.upstream_proofs import grant_fresh_upstream_access


@pytest.fixture
def workspace(db):
    tenant = Tenant.objects.create(
        provider="commcare",
        external_id="graph-domain",
        canonical_name="Graph Domain",
    )
    ws = Workspace.objects.create(name="Graph Domain")
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def member_user(db, workspace):
    user = User.objects.create_user(email="graph@example.com", password="pass")
    grant_fresh_upstream_access(user, workspace.tenant)
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.MANAGE)
    return user


@pytest.fixture
def member_client(member_user):
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(member_user)
    finally:
        user_logged_in.connect(update_last_login)
    return client


def graph_doc():
    return {
        "schema_version": 1,
        "name": "Visits",
        "blocks": [
            {"id": "range", "type": "date_filter", "config": {"default": "last_30_days"}},
            {
                "id": "q",
                "type": "semantic_query",
                "hidden": True,
                "inputs": {"date_range": {"$ref": "range.value"}},
                "config": {
                    "queries": {
                        "visits_by_day": {
                            "measures": ["visits.count"],
                            "time_dimension": "visits.visit_date",
                            "granularity": "day",
                            "limit": 100,
                        }
                    }
                },
            },
            {
                "id": "chart",
                "type": "graph",
                "inputs": {"data": {"$ref": "q.visits_by_day"}},
                "config": {
                    "title": "Visits by day",
                    "chart_type": "line",
                    "x_key": "date",
                    "series": ["visits_count"],
                },
            },
        ],
    }


@pytest.fixture
def static_story_doc():
    return {
        "schema_version": 1,
        "name": "Description smoke test",
        "prd": "Rendered scope is separate from the library description.",
        "blocks": [
            {"id": "intro", "type": "markdown", "config": {"body": "Original content"}},
        ],
    }


@pytest.fixture
def graph_tools(workspace, member_user):
    return {tool.name: tool for tool in create_artifact_graph_tools(workspace, member_user)}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description_args", "expected"),
    [
        pytest.param({}, "", id="omitted"),
        pytest.param({"description": None}, "", id="null"),
        pytest.param({"description": ""}, "", id="empty"),
        pytest.param({"description": "  New description\n"}, "New description", id="trimmed"),
    ],
)
async def test_graph_manager_create_description(
    graph_tools, static_story_doc, description_args, expected
):
    result = await graph_tools["artifact_write"].ainvoke(
        {
            "action": "create",
            "title": "Description smoke test",
            "story_doc": static_story_doc,
            **description_args,
        }
    )

    assert result["status"] == "created"
    artifact = await Artifact.objects.aget(id=result["artifact"]["id"])
    assert artifact.description == expected
    assert result["artifact"]["description"] == expected
    assert result["runtime"]["success"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["apply", "replace"])
@pytest.mark.parametrize(
    ("description_args", "expected"),
    [
        pytest.param({}, "Original description", id="omitted-preserves"),
        pytest.param({"description": None}, "Original description", id="null-preserves"),
        pytest.param(
            {"description": "  Updated description\n"}, "Updated description", id="updates"
        ),
        pytest.param({"description": ""}, "", id="empty-clears"),
        pytest.param({"description": " \n "}, "", id="whitespace-clears"),
    ],
)
async def test_graph_manager_description_edits_round_trip(
    graph_tools, static_story_doc, member_client, workspace, action, description_args, expected
):
    write = graph_tools["artifact_write"]
    created = await write.ainvoke(
        {
            "action": "create",
            "title": "Description smoke test",
            "description": "Original description",
            "story_doc": static_story_doc,
        }
    )
    original_id = created["artifact"]["id"]
    edit = {"action": action, "artifact_id": original_id, **description_args}
    if action == "apply":
        edit["ops"] = [
            {"op": "set", "target": "block/intro/config/body", "value": "Revised content"}
        ]
    else:
        revised_doc = deepcopy(static_story_doc)
        revised_doc["blocks"][0]["config"]["body"] = "Revised content"
        edit["story_doc"] = revised_doc

    result = await write.ainvoke(edit)

    assert result["status"] == ("updated" if action == "apply" else "replaced")
    latest_id = result["artifact"]["id"]
    latest = await Artifact.objects.aget(id=latest_id)
    original = await Artifact.objects.aget(id=original_id)
    assert latest.description == expected
    assert original.description == "Original description"
    assert original.data["story_doc"]["blocks"][0]["config"]["body"] == "Original content"
    assert latest.parent_artifact_id == original.id
    assert latest.version == 2
    assert latest.data["story_doc"]["blocks"][0]["config"]["body"] == "Revised content"
    assert latest.data["story_doc"]["prd"] == static_story_doc["prd"]
    assert result["artifact"]["description"] == expected
    assert result["runtime"]["success"] is True

    # The agent must be able to verify metadata, not infer it from a successful write.
    overview = await graph_tools["artifact_graph_overview"].ainvoke({"artifact_id": latest_id})
    dependencies = await graph_tools["get_artifact_semantic_queries"].ainvoke(
        {"artifact_id": latest_id}
    )
    checked = await write.ainvoke({"action": "check", "artifact_id": latest_id})
    for payload in (overview, dependencies, checked):
        assert payload["artifact"]["description"] == expected

    # Re-fetch via the API used by the actual card; only the latest revision is listed.
    response = await member_client.get(f"/api/workspaces/{workspace.id}/artifacts/")
    assert response.status_code == 200
    cards = response.json()["results"]
    assert [(card["id"], card["version"], card["description"]) for card in cards] == [
        (latest_id, 2, expected)
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("ops_args", [{}, {"ops": []}], ids=["omitted-ops", "empty-ops"])
@pytest.mark.parametrize("description", ["Metadata only", ""])
async def test_graph_manager_description_only_edit(
    graph_tools, static_story_doc, ops_args, description
):
    write = graph_tools["artifact_write"]
    created = await write.ainvoke(
        {
            "action": "create",
            "title": "Description smoke test",
            "description": "Original description",
            "story_doc": static_story_doc,
        }
    )
    original = await Artifact.objects.aget(id=created["artifact"]["id"])

    result = await write.ainvoke(
        {
            "action": "apply",
            "artifact_id": created["artifact"]["id"],
            "description": description,
            **ops_args,
        }
    )

    assert result["status"] == "updated"
    latest = await Artifact.objects.aget(id=result["artifact"]["id"])
    assert latest.description == description
    assert latest.data["story_doc"] == original.data["story_doc"]
    assert latest.version == 2
    assert result["runtime"]["success"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edit_args", "expected_message"),
    [
        pytest.param({}, "ops or description are required for apply.", id="missing"),
        pytest.param(
            {"description": None}, "ops or description are required for apply.", id="null"
        ),
        pytest.param(
            {"description": "Changed", "ops": [{"op": "invalid"}]},
            "Unsupported op 'invalid'",
            id="invalid-op",
        ),
    ],
)
async def test_graph_manager_rejects_missing_or_invalid_description_edit(
    graph_tools, static_story_doc, edit_args, expected_message
):
    write = graph_tools["artifact_write"]
    created = await write.ainvoke(
        {
            "action": "create",
            "title": "Description smoke test",
            "description": "Original description",
            "story_doc": static_story_doc,
        }
    )
    result = await write.ainvoke(
        {"action": "apply", "artifact_id": created["artifact"]["id"], **edit_args}
    )

    assert result["status"] == "error"
    assert result["message"] == expected_message
    assert await Artifact.all_objects.acount() == 1
    original = await Artifact.objects.aget(id=created["artifact"]["id"])
    assert original.description == "Original description"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["apply", "replace"])
async def test_graph_manager_failed_description_edit_preserves_original(
    graph_tools, static_story_doc, action
):
    write = graph_tools["artifact_write"]
    created = await write.ainvoke(
        {
            "action": "create",
            "title": "Description smoke test",
            "description": "Original description",
            "story_doc": static_story_doc,
        }
    )
    edit = {
        "action": action,
        "artifact_id": created["artifact"]["id"],
        "description": "Must not be published",
    }
    if action == "replace":
        edit["story_doc"] = static_story_doc
    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(return_value={"success": False, "summary": "Runtime failure"}),
    ) as check:
        result = await write.ainvoke(edit)

    check.assert_awaited_once()
    assert result["status"] == "error"
    assert await Artifact.objects.acount() == 1
    original = await Artifact.objects.aget(id=created["artifact"]["id"])
    assert original.description == "Original description"
    assert (
        await Artifact.all_objects.filter(parent_artifact=original, is_deleted=True).acount() == 1
    )


def test_graph_doc_rejects_empty_blocks():
    diagnostics = validate_doc({"schema_version": 1, "blocks": []})

    assert {item.get("code") for item in diagnostics} == {"doc_empty_blocks"}


def test_graph_doc_rejects_raw_query_keys_and_missing_time_dimension():
    doc = {
        "schema_version": 1,
        "blocks": [
            {"id": "range", "type": "date_filter", "config": {}},
            {
                "id": "q",
                "type": "semantic_query",
                "inputs": {"date_range": {"$ref": "range.value"}},
                "config": {
                    "queries": {
                        "bad": {
                            "measures": ["visits.count"],
                            "timeDimensions": [{"dimension": "visits.visit_date"}],
                        }
                    }
                },
            },
        ],
    }

    codes = {item["code"] for item in validate_doc(doc)}

    assert "raw_query_key" in codes
    assert "query_window_without_time_dimension" in codes


def test_graph_doc_unknown_config_key_reports_allowed_keys():
    doc = {
        "schema_version": 1,
        "blocks": [
            {"id": "intro", "type": "markdown", "config": {"text": "Wrong key"}},
            {"id": "summary", "type": "tldr", "config": {"text": "Wrong key"}},
        ],
    }

    diagnostics = [item for item in validate_doc(doc) if item.get("code") == "unknown_config_key"]

    assert len(diagnostics) == 2
    messages_by_block = {item["block_id"]: item["message"] for item in diagnostics}
    assert "Allowed config keys for markdown: body, content" in messages_by_block["intro"]
    assert "Allowed config keys for tldr: content, items" in messages_by_block["summary"]


def test_graph_doc_allows_recharts_graph_config_keys():
    doc = graph_doc()
    doc["blocks"][2]["config"].update(
        {
            "recharts": {"type": "BarChart", "children": []},
            "stacked": True,
            "y_format": "compact",
            "height": 320,
        }
    )

    codes = {item.get("code") for item in validate_doc(doc)}

    assert "unknown_config_key" not in codes


def test_graph_doc_allows_bounded_graph_style_and_stat_comparison():
    doc = graph_doc()
    doc["blocks"][2]["config"].update(
        {
            "subtitle": "Weekly visits",
            "style": {
                "palette": "categorical",
                "legend": "top",
                "grid": "horizontal",
                "curve": "monotone",
                "orientation": "vertical",
                "labels": "none",
            },
        }
    )
    doc["blocks"].append(
        {
            "id": "stat",
            "type": "stat",
            "inputs": {"current": {"$ref": "q.visits_by_day"}},
            "config": {
                "label": "Visits",
                "value_key": "visits_count",
                "prefix": "~",
                "suffix": " visits",
                "comparison": {
                    "type": "percent",
                    "format": "percent_1",
                    "label": "vs previous period",
                    "goal": "higher",
                },
            },
        }
    )
    doc["blocks"].extend(
        [
            {
                "id": "question",
                "type": "question",
                "config": {"text": "What changed?", "context": "Compare the selected periods."},
            },
            {
                "id": "summary",
                "type": "tldr",
                "config": {"title": "Key findings", "items": ["Approvals rose."]},
            },
        ]
    )

    diagnostics = validate_doc(doc)

    assert not [
        item
        for item in diagnostics
        if item.get("code") in {"unknown_config_key", "config_shape", "config_value"}
    ]


def test_graph_doc_rejects_unknown_visualization_grammar_values():
    doc = graph_doc()
    doc["blocks"][2]["config"].update(
        {
            "chart_type": "radar",
            "style": {"palette": "rainbow", "animation": "sparkle"},
        }
    )

    diagnostics = validate_doc(doc)
    codes = {item.get("code") for item in diagnostics}

    assert "graph_chart_type" in codes
    assert "config_value" in codes
    assert "unknown_config_key" in codes


def test_graph_doc_rejects_recharts_data_prop_refs():
    doc = graph_doc()
    doc["blocks"][2]["config"]["recharts"] = {
        "type": "PieChart",
        "children": [
            {
                "type": "Pie",
                "props": {
                    "data": {"$ref": "q.visits_by_day"},
                    "dataKey": "visits_count",
                    "nameKey": "date",
                },
            }
        ],
    }

    diagnostics = validate_doc(doc)

    assert {
        item.get("block_id") for item in diagnostics if item.get("code") == "recharts_data_prop"
    } == {"chart"}


def test_graph_doc_rejects_unsafe_recharts_props_and_colors():
    doc = graph_doc()
    doc["blocks"][2]["config"]["recharts"] = {
        "type": "BarChart",
        "props": {"style": {"position": "fixed", "inset": 0, "zIndex": 9999}},
        "palette": ["#ff0000"],
        "children": [
            {
                "type": "Bar",
                "props": {"dataKey": "visits_count", "fill": "red", "onClick": "alert"},
            }
        ],
    }

    diagnostics = validate_doc(doc)
    codes = {item.get("code") for item in diagnostics}

    assert "recharts_prop" in codes
    assert "recharts_color" in codes


def test_graph_doc_allows_bounded_raw_recharts_composition():
    doc = graph_doc()
    doc["blocks"][2]["config"]["recharts"] = {
        "type": "ComposedChart",
        "props": {"margin": {"top": 8, "right": 12, "bottom": 8, "left": 0}},
        "children": [
            {"type": "CartesianGrid", "props": {"stroke": "var(--border)", "vertical": False}},
            {"type": "XAxis", "props": {"dataKey": "date", "axisLine": False, "tickLine": False}},
            {"type": "YAxis", "props": {"yAxisId": "count", "allowDecimals": False}},
            {"type": "YAxis", "props": {"yAxisId": "amount", "orientation": "right"}},
            {
                "type": "Line",
                "props": {
                    "dataKey": "visits_count",
                    "yAxisId": "count",
                    "stroke": "var(--chart-1)",
                },
            },
        ],
    }

    codes = {item.get("code") for item in validate_doc(doc)}

    assert "recharts_prop" not in codes
    assert "recharts_color" not in codes


def test_graph_doc_passes_cube_filter_operators_to_runtime():
    doc = graph_doc()
    query = doc["blocks"][1]["config"]["queries"]["visits_by_day"]
    query["filters"] = [
        {
            "field": "visits.visit_date",
            "operator": "afterDate",
            "value": "2026-06-01",
        }
    ]

    codes = {item.get("code") for item in validate_doc(doc)}

    assert "query_filter_operator" not in codes


def test_graph_doc_rejects_missing_bound_result_key():
    doc = graph_doc()
    doc["blocks"][2]["config"]["x_key"] = "verification"
    doc["blocks"][2]["config"]["transform"] = {
        "type": "bucket",
        "source_key": "visits_status",
        "target_key": "verification",
    }

    diagnostics = validate_doc(doc)

    assert {(item.get("severity"), item.get("code")) for item in diagnostics} >= {
        ("error", "missing_result_key:verification"),
        ("error", "unknown_config_key"),
    }


def test_apply_ops_rejects_introduced_diagnostics():
    with pytest.raises(GraphDocError, match="introduced diagnostics"):
        apply_ops(
            {"schema_version": 1, "blocks": []},
            [
                {
                    "op": "add_block",
                    "after": "end",
                    "block": {
                        "id": "chart",
                        "type": "graph",
                        "config": {"title": "Missing data"},
                    },
                }
            ],
        )


@pytest.mark.django_db
def test_sync_manifest_persists_dependency_rows(workspace, member_user):
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread",
        data={"story_doc": graph_doc()},
    )

    manifest = sync_artifact_semantic_query_manifest(artifact)

    assert len(manifest["entries"]) == 1
    assert artifact.semantic_queries[0]["name"] == "q.visits_by_day"
    row = ArtifactSemanticQuery.objects.get(artifact=artifact)
    assert row.query_key == "q.visits_by_day"
    assert row.members == ["visits.count", "visits.visit_date"]
    assert row.datasets == ["visits"]


@pytest.mark.django_db
def test_manifest_result_keys_are_only_query_outputs(workspace, member_user):
    doc = {
        "schema_version": 1,
        "blocks": [
            {
                "id": "q",
                "type": "semantic_query",
                "config": {
                    "queries": {
                        "bucketed": {
                            "measures": ["visits.count"],
                            "time_dimension": "visits.visit_date",
                            "granularity": "day",
                            "filters": [
                                {
                                    "field": "visits.status",
                                    "operator": "equals",
                                    "value": "approved",
                                }
                            ],
                            "order_by": [{"field": "visits.visit_date", "direction": "asc"}],
                        }
                    }
                },
            }
        ],
    }
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread",
        data={"story_doc": doc},
    )

    manifest = sync_artifact_semantic_query_manifest(artifact)
    entry = manifest["entries"][0]

    assert entry["members"] == [
        "visits.count",
        "visits.status",
        "visits.visit_date",
    ]
    assert entry["result_keys"] == ["date", "visits_count"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_semantic_query_dependency_api_paginates(
    workspace,
    member_user,
    member_client,
):
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread",
        data={"story_doc": graph_doc()},
    )

    url = f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/semantic-queries/?limit=1"
    response = await member_client.get(url)

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total_count"] == 1
    assert payload["pagination"]["has_more"] is False
    assert payload["semantic_queries"][0]["query_key"] == "q.visits_by_day"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_semantic_query_dependency_api_is_read_only_for_viewers(
    workspace, member_user, member_client, invalid
):
    await WorkspaceMembership.objects.filter(workspace=workspace, user=member_user).aupdate(
        role=WorkspaceRole.READ
    )
    doc = graph_doc()
    queries = doc["blocks"][1]["config"]["queries"]
    # Mixed case diverges between Python and most DB collations, pinning DB ordering.
    queries["Zeta"] = deepcopy(queries["visits_by_day"])
    queries["alpha"] = deepcopy(queries["visits_by_day"])
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": doc},
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(artifact)
    original_queries = deepcopy(artifact.semantic_queries)
    original_manifest = deepcopy(artifact.semantic_query_manifest)
    rows = [row async for row in ArtifactSemanticQuery.objects.filter(artifact=artifact)]
    original_rows = [
        row async for row in ArtifactSemanticQuery.objects.filter(artifact=artifact).values()
    ]
    if invalid:
        # A drifted catalog: re-syncing would persist an empty semantic_queries.
        for query in queries.values():
            del query["time_dimension"]
        await Artifact.objects.filter(pk=artifact.pk).aupdate(data={"story_doc": doc})

    url = f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/semantic-queries/?limit=2"
    response = await member_client.get(url)

    assert response.status_code == 200
    await artifact.arefresh_from_db()
    assert artifact.semantic_queries == original_queries
    assert artifact.semantic_query_manifest == original_manifest
    assert [
        row async for row in ArtifactSemanticQuery.objects.filter(artifact=artifact).values()
    ] == original_rows
    payload = response.json()
    assert payload["pagination"] == {
        "limit": 2,
        "offset": 0,
        "count": 2,
        "total_count": 3,
        "has_more": True,
    }
    returned = payload["semantic_queries"]
    assert [r["query_key"] for r in returned] == [row.query_key for row in rows[:2]]
    if invalid:
        assert {r["validation_status"] for r in returned} == {"invalid"}
        assert payload["manifest"]["unresolved_count"] > 0
    else:
        assert returned == [semantic_query_summary(row) for row in rows[:2]]
        assert payload["manifest"]["entry_count"] == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_semantic_query_dependency_api_does_not_create_cache(
    workspace, member_user, member_client
):
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Uncached",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": graph_doc()},
    )

    url = f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/semantic-queries/"
    response = await member_client.get(url)

    assert response.status_code == 200
    assert response.json()["semantic_queries"][0]["query_key"] == "q.visits_by_day"
    await artifact.arefresh_from_db()
    assert artifact.semantic_query_manifest == {}
    assert artifact.semantic_queries == []
    assert not await ArtifactSemanticQuery.objects.filter(artifact=artifact).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_manager_creates_story_and_generic_tool_rejects_story(workspace, member_user):
    graph_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, "thread")
        if item.name == "artifact_write"
    )
    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(return_value={"summary": "0/0 queries ok"}),
    ):
        result = await graph_tool.ainvoke(
            {
                "action": "create",
                "title": "Visits",
                "story_doc": graph_doc(),
                "run_check": True,
            }
        )

    assert result["status"] == "created"
    assert await Artifact.objects.filter(artifact_type=ArtifactType.STORY).acount() == 1

    overview_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, "thread")
        if item.name == "artifact_graph_overview"
    )
    overview = await overview_tool.ainvoke({"artifact_id": result["artifact"]["id"]})
    assert overview["status"] == "ok"
    assert overview["story_doc"] == graph_doc()
    assert overview["doc"]["block_count"] == len(graph_doc()["blocks"])

    dependencies_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, "thread")
        if item.name == "get_artifact_semantic_queries"
    )
    dependencies = await dependencies_tool.ainvoke(
        {"artifact_id": result["artifact"]["id"], "limit": 1, "offset": 0}
    )
    assert dependencies["status"] == "ok"
    assert dependencies["pagination"]["has_more"] is False
    assert dependencies["semantic_queries"][0]["query_key"] == "q.visits_by_day"

    create_tool = next(
        item
        for item in create_artifact_tools(workspace, member_user, "thread")
        if item.name == "create_artifact"
    )
    rejected = await create_tool.ainvoke(
        {
            "title": "Direct Story",
            "artifact_type": "story",
            "data": {"story_doc": graph_doc()},
        }
    )
    assert rejected["status"] == "error"
    assert "artifact_manager" in rejected["message"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_manager_rejects_missing_or_empty_story_doc(workspace, member_user):
    graph_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, "thread")
        if item.name == "artifact_write"
    )

    missing = await graph_tool.ainvoke(
        {
            "action": "create",
            "title": "Missing doc",
            "run_check": False,
        }
    )
    empty = await graph_tool.ainvoke(
        {
            "action": "create",
            "title": "Empty doc",
            "story_doc": {"schema_version": 1, "blocks": []},
            "run_check": False,
        }
    )

    assert missing["status"] == "error"
    assert missing["message"] == "story_doc is required for create."
    assert empty["status"] == "error"
    assert {item.get("code") for item in empty["diagnostics"]} == {"doc_empty_blocks"}
    assert await Artifact.objects.filter(artifact_type=ArtifactType.STORY).acount() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_manager_replace_and_apply_create_linked_versions(workspace, member_user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=member_user,
        title="Edit a story",
    )
    original = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        description="Visit trends",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id=str(thread.id),
        data={"story_doc": graph_doc()},
    )
    graph_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, str(thread.id))
        if item.name == "artifact_write"
    )

    replacement_doc = graph_doc()
    replacement_doc["blocks"][2]["config"]["title"] = "Replacement title"
    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(return_value={"success": True, "summary": "1/1 queries ok"}),
    ):
        replaced = await graph_tool.ainvoke(
            {
                "action": "replace",
                "artifact_id": str(original.id),
                "story_doc": replacement_doc,
            }
        )
        updated = await graph_tool.ainvoke(
            {
                "action": "apply",
                "artifact_id": replaced["artifact"]["id"],
                "ops": [
                    {
                        "op": "set",
                        "target": "block/chart/config/title",
                        "value": "Applied title",
                    }
                ],
            }
        )

    assert replaced["status"] == "replaced"
    assert updated["status"] == "updated"
    replacement = await Artifact.objects.aget(id=replaced["artifact"]["id"])
    applied = await Artifact.objects.aget(id=updated["artifact"]["id"])
    assert replacement.parent_artifact_id == original.id
    assert replacement.version == 2
    assert replacement.description == original.description
    assert applied.parent_artifact_id == replacement.id
    assert applied.version == 3
    assert applied.data["story_doc"]["blocks"][2]["config"]["title"] == "Applied title"
    assert await ThreadArtifact.objects.filter(thread=thread, artifact=replacement).aexists()
    assert await ThreadArtifact.objects.filter(thread=thread, artifact=applied).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_manager_runtime_invalid_replace_keeps_previous_version(
    workspace,
    member_user,
):
    original = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread",
        data={"story_doc": graph_doc()},
    )
    graph_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, "thread")
        if item.name == "artifact_write"
    )

    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(return_value={"success": False, "summary": "0/1 queries ok"}),
    ):
        result = await graph_tool.ainvoke(
            {
                "action": "replace",
                "artifact_id": str(original.id),
                "story_doc": graph_doc(),
            }
        )

    assert result["status"] == "error"
    assert await Artifact.objects.filter(id=original.id).aexists()
    assert await Artifact.objects.filter(parent_artifact=original).acount() == 0
    assert (
        await Artifact.all_objects.filter(
            parent_artifact=original,
            is_deleted=True,
        ).acount()
        == 1
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_manager_does_not_publish_runtime_invalid_create(workspace, member_user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=member_user,
        title="Runtime invalid artifact",
    )
    graph_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, str(thread.id))
        if item.name == "artifact_write"
    )

    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(
            return_value={
                "success": False,
                "diagnostics": [
                    {
                        "severity": "error",
                        "message": "Data key missing",
                        "code": "missing_result_key:value",
                    }
                ],
                "summary": "0/1 queries ok",
            }
        ),
    ):
        result = await graph_tool.ainvoke(
            {
                "action": "create",
                "title": "Runtime invalid",
                "story_doc": graph_doc(),
                "run_check": True,
            }
        )

    assert result["status"] == "error"
    assert "not published" in result["message"]
    assert await Artifact.objects.filter(artifact_type=ArtifactType.STORY).acount() == 0
    assert (
        await Artifact.all_objects.filter(
            artifact_type=ArtifactType.STORY, is_deleted=True
        ).acount()
        == 1
    )
    assert await ThreadArtifact.objects.filter(thread=thread).acount() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_graph_manager_ignores_run_check_false_for_writes(workspace, member_user):
    thread = await Thread.objects.acreate(
        workspace=workspace,
        user=member_user,
        title="Runtime invalid artifact",
    )
    graph_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, str(thread.id))
        if item.name == "artifact_write"
    )

    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(
            return_value={
                "success": False,
                "diagnostics": [],
                "key_warnings": [
                    {
                        "query_key": "q.visits_by_day",
                        "message": "Expected result key missing",
                    }
                ],
                "summary": "1/1 queries ok",
            }
        ),
    ):
        result = await graph_tool.ainvoke(
            {
                "action": "create",
                "title": "Runtime invalid",
                "story_doc": graph_doc(),
                "run_check": False,
            }
        )

    assert result["status"] == "error"
    assert "not published" in result["message"]
    assert result["runtime"]["key_warnings"]
    assert await Artifact.objects.filter(artifact_type=ArtifactType.STORY).acount() == 0
    assert (
        await Artifact.all_objects.filter(
            artifact_type=ArtifactType.STORY, is_deleted=True
        ).acount()
        == 1
    )
    assert await ThreadArtifact.objects.filter(thread=thread).acount() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_check_graph_artifact_loads_workspace_in_async_context(workspace, member_user):
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread",
        data={"story_doc": graph_doc()},
    )
    artifact = await Artifact.objects.aget(pk=artifact.pk)

    with patch(
        "apps.artifacts.services.graph_runtime.run_semantic_query",
        new=AsyncMock(
            return_value={
                "success": True,
                "columns": ["date", "visits_count"],
                "rows": [{"date": "2026-01-01", "visits_count": 1}],
                "row_count": 1,
                "truncated": False,
            }
        ),
    ) as query:
        result = await check_graph_artifact(artifact, user_id=str(member_user.id))

    assert result["summary"] == "1/1 queries ok"
    assert query.await_args.args[0].id == workspace.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_read_dependency_inspection_does_not_rewrite_artifact(
    workspace, member_user, invalid
):
    await WorkspaceMembership.objects.filter(workspace=workspace, user=member_user).aupdate(
        role=WorkspaceRole.READ
    )
    thread = await Thread.objects.acreate(workspace=workspace, user=member_user, title="Inspect")
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": graph_doc()},
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(artifact)
    original_queries = deepcopy(artifact.semantic_queries)
    original_manifest = deepcopy(artifact.semantic_query_manifest)
    original_rows = [
        row async for row in ArtifactSemanticQuery.objects.filter(artifact=artifact).values()
    ]
    if invalid:
        doc = graph_doc()
        del doc["blocks"][1]["config"]["queries"]["visits_by_day"]["time_dimension"]
        await Artifact.objects.filter(pk=artifact.pk).aupdate(data={"story_doc": doc})
    tools = {t.name: t for t in create_artifact_graph_tools(workspace, member_user, str(thread.id))}

    result = await tools["get_artifact_semantic_queries"].ainvoke(
        {"artifact_id": str(artifact.id), "limit": 1}
    )

    await artifact.arefresh_from_db()
    assert artifact.semantic_queries == original_queries
    assert artifact.semantic_query_manifest == original_manifest
    assert [
        row async for row in ArtifactSemanticQuery.objects.filter(artifact=artifact).values()
    ] == original_rows
    assert result["pagination"] == {"limit": 1, "offset": 0, "total_count": 1, "has_more": False}
    record = result["semantic_queries"][0]
    assert record["query_key"] == "q.visits_by_day"
    assert record["validation_status"] == ("invalid" if invalid else "valid")
    assert record["id"] == (None if invalid else str(original_rows[0]["id"]))
    assert record["created_at"] == (None if invalid else original_rows[0]["created_at"].isoformat())
    assert await ThreadArtifact.objects.filter(
        thread=thread, artifact=artifact, source="mentioned"
    ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_read_dependency_inspection_paginates_without_creating_cache(workspace, member_user):
    await WorkspaceMembership.objects.filter(workspace=workspace, user=member_user).aupdate(
        role=WorkspaceRole.READ
    )
    doc = graph_doc()
    queries = doc["blocks"][1]["config"]["queries"]
    queries["aaa"] = deepcopy(queries["visits_by_day"])
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Uncached",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": doc},
    )
    tool = next(
        t
        for t in create_artifact_graph_tools(workspace, member_user)
        if t.name == "get_artifact_semantic_queries"
    )
    first = await tool.ainvoke({"artifact_id": str(artifact.id), "limit": 1})
    second = await tool.ainvoke({"artifact_id": str(artifact.id), "limit": 1, "offset": 1})
    empty = await tool.ainvoke({"artifact_id": str(artifact.id), "offset": 2})
    assert first["semantic_queries"][0]["query_key"] == "q.aaa"
    assert first["pagination"]["has_more"] is True
    assert second["semantic_queries"][0]["query_key"] == "q.visits_by_day"
    assert second["pagination"]["has_more"] is False
    assert empty["semantic_queries"] == []
    for result in (first, second):
        record = result["semantic_queries"][0]
        assert (
            record["id"] is None and record["created_at"] is None and record["updated_at"] is None
        )
        assert record["query_payload"]["measures"] == ["visits.count"]
        assert result["pagination"]["total_count"] == 2
    await artifact.arefresh_from_db()
    assert artifact.semantic_query_manifest == {}
    assert artifact.semantic_queries == []
    assert not await ArtifactSemanticQuery.objects.filter(artifact=artifact).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_dependency_tool_and_api_page_in_the_same_order(
    workspace, member_user, member_client
):
    doc = graph_doc()
    queries = doc["blocks"][1]["config"]["queries"]
    # Mixed case sorts differently under Python and most DB collations.
    queries["Zeta"] = deepcopy(queries["visits_by_day"])
    queries["alpha"] = deepcopy(queries["visits_by_day"])
    artifact = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        title="Visits",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": doc},
    )
    await sync_to_async(sync_artifact_semantic_query_manifest, thread_sensitive=True)(artifact)
    persisted = [
        row.query_key async for row in ArtifactSemanticQuery.objects.filter(artifact=artifact)
    ]
    tool = next(
        t
        for t in create_artifact_graph_tools(workspace, member_user)
        if t.name == "get_artifact_semantic_queries"
    )

    from_tool = await tool.ainvoke({"artifact_id": str(artifact.id)})
    url = f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/semantic-queries/"
    response = await member_client.get(url)
    assert response.status_code == 200
    from_api = response.json()

    tool_keys = [r["query_key"] for r in from_tool["semantic_queries"]]
    assert tool_keys == [r["query_key"] for r in from_api["semantic_queries"]]
    assert tool_keys == persisted
