import threading
from copy import deepcopy
from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.core.management import call_command
from django.db import connection
from django.test import AsyncClient, Client
from django.test.utils import CaptureQueriesContext

from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.artifacts.models import Artifact, ArtifactSemanticQuery, ArtifactType
from apps.artifacts.services.graph_doc import GraphDocError, apply_ops, validate_doc
from apps.artifacts.services.graph_manifest import (
    backfill_missing_semantic_query_manifest,
    semantic_query_summary,
    sync_artifact_semantic_query_manifest,
)
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.chat.models import Thread, ThreadArtifact
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from tests.tenant_access import usable_connection


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
    tenant = workspace.tenants.get()
    user = User.objects.create_user(email="graph@example.com", password="pass")
    TenantMembership.objects.create(
        user=user,
        tenant=tenant,
        connection=usable_connection(user, tenant.provider),
    )
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
    assert result["ui_path"] == f"/workspaces/{artifact.workspace_id}/artifacts/{artifact.id}"
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
    assert checked["ui_path"] == f"/workspaces/{workspace.id}/artifacts/{latest_id}"
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
    assert result["runtime"] is None
    assert result["runtime_validation"] == "not_required_metadata_only"


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
async def test_graph_manager_failed_document_edit_preserves_original(
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
        revised = deepcopy(static_story_doc)
        revised["blocks"][0]["config"]["body"] = "Changed document"
        edit["story_doc"] = revised
    else:
        edit["ops"] = [
            {"op": "set", "target": "block/intro/config/body", "value": "Changed document"}
        ]
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


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["apply", "replace"])
@pytest.mark.parametrize("description", ["New description", ""])
async def test_metadata_edit_does_not_query_unavailable_data(
    graph_tools, workspace, member_user, action, description
):
    doc = graph_doc()
    original = await Artifact.objects.acreate(
        workspace=workspace,
        created_by=member_user,
        artifact_type=ArtifactType.STORY,
        title=doc["name"],
        description="Original description",
        data={"story_doc": doc},
    )
    edit = {"action": action, "artifact_id": str(original.id), "description": description}
    if action == "replace":
        edit["story_doc"] = doc
    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(side_effect=AssertionError("Metadata edits must not contact Cube")),
    ) as check:
        result = await graph_tools["artifact_write"].ainvoke(edit)

    check.assert_not_awaited()
    assert result["status"] == ("updated" if action == "apply" else "replaced")
    assert result["runtime"] is None  # Do not claim a fresh successful data check.
    assert result["runtime_validation"] == "not_required_metadata_only"
    latest = await Artifact.objects.aget(id=result["artifact"]["id"])
    assert latest.data == original.data
    assert latest.description == description
    assert latest.parent_artifact_id == original.id
    assert latest.version == original.version + 1
    assert await latest.semantic_query_records.acount() > 0
    await original.arefresh_from_db()
    assert original.description == "Original description"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_metadata_edit_cannot_skip_static_document_validation(graph_tools, workspace):
    original = await Artifact.objects.acreate(
        workspace=workspace,
        artifact_type=ArtifactType.STORY,
        title="Invalid old artifact",
        data={"story_doc": {"schema_version": 1, "name": "Invalid", "blocks": []}},
    )
    result = await graph_tools["artifact_write"].ainvoke(
        {"action": "apply", "artifact_id": str(original.id), "description": "Description"}
    )
    assert result["status"] == "error"
    assert result["diagnostics"]
    assert await Artifact.all_objects.acount() == 1


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


@pytest.mark.parametrize(
    "root_type",
    ["AreaChart", "BarChart", "ComposedChart", "LineChart", "PieChart", "ScatterChart"],
)
def test_graph_doc_accepts_each_renderer_supported_recharts_root(root_type):
    doc = graph_doc()
    doc["blocks"][2]["config"]["recharts"] = {"type": root_type}

    assert "recharts_root_type" not in {item.get("code") for item in validate_doc(doc)}


@pytest.mark.parametrize("root_type", ["XAxis", "Line", "Legend", "Cell"])
def test_graph_doc_rejects_recharts_root_the_renderer_cannot_mount(root_type):
    doc = graph_doc()
    doc["blocks"][2]["config"]["recharts"] = {"type": root_type, "props": {"dataKey": "date"}}

    diagnostics = [item for item in validate_doc(doc) if item.get("code") == "recharts_root_type"]

    assert len(diagnostics) == 1
    assert diagnostics[0]["block_id"] == doc["blocks"][2]["id"]
    assert "ComposedChart" in diagnostics[0]["message"]


def test_graph_doc_allows_chart_components_nested_below_the_root():
    doc = graph_doc()
    doc["blocks"][2]["config"]["recharts"] = {
        "type": "ComposedChart",
        "children": [{"type": "XAxis", "props": {"dataKey": "date"}}],
    }

    assert "recharts_root_type" not in {item.get("code") for item in validate_doc(doc)}


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
async def test_graph_manager_creates_story_and_exposes_overview_and_dependencies(
    workspace, member_user
):
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
    assert replaced["ui_path"] == f"/workspaces/{workspace.id}/artifacts/{replacement.id}"
    assert updated["ui_path"] == f"/workspaces/{workspace.id}/artifacts/{applied.id}"
    assert replacement.id != original.id
    assert applied.id != replacement.id
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

    replacement = graph_doc()
    replacement["blocks"][1]["config"]["queries"]["visits_by_day"]["limit"] = 99
    with patch(
        "apps.agents.tools.artifact_graph_tool.check_graph_artifact",
        new=AsyncMock(return_value={"success": False, "summary": "0/1 queries ok"}),
    ):
        result = await graph_tool.ainvoke(
            {
                "action": "replace",
                "artifact_id": str(original.id),
                "story_doc": replacement,
                "run_check": False,
            }
        )

    assert result["status"] == "error"
    assert "ui_path" not in result
    assert "ui_path" not in result["artifact"]
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
    assert "ui_path" not in result
    assert "ui_path" not in result["artifact"]
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
        "apps.artifacts.services.query_batch.run_semantic_query",
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


WRITE_STATEMENTS = ("INSERT", "UPDATE", "DELETE")


def _manifest_less_story(workspace, user):
    """A story saved before manifests existed: both manifest fields empty."""
    return Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        title="Legacy",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": graph_doc()},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("endpoint", ["data", "query-data", "recovery"])
def test_read_member_get_of_manifest_less_story_writes_nothing(workspace, member_user, endpoint):
    WorkspaceMembership.objects.filter(workspace=workspace, user=member_user).update(
        role=WorkspaceRole.READ
    )
    artifact = _manifest_less_story(workspace, member_user)
    before = list(Artifact.objects.filter(pk=artifact.pk).values())
    client = Client()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(member_user)
    finally:
        user_logged_in.connect(update_last_login)

    async def execute(_workspace, query, **kwargs):
        return {"columns": ["visits_count"], "rows": [[3]], "row_count": 1, "semantic_query": query}

    url = f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/{endpoint}/"
    with (
        patch("apps.artifacts.services.query_batch.run_semantic_query", side_effect=execute),
        patch(
            "apps.artifacts.views.current_artifact_data_state",
            new=AsyncMock(return_value={"queryable": True, "status": "ready"}),
        ),
        CaptureQueriesContext(connection) as captured,
    ):
        response = client.get(url)

    assert response.status_code == 200, response.content
    # The view's telemetry event is the one write a read may make; it touches no artifact.
    writes = [
        q["sql"]
        for q in captured
        if q["sql"].lstrip().upper().startswith(WRITE_STATEMENTS)
        and '"telemetry_telemetryevent"' not in q["sql"]
    ]
    assert writes == []
    assert list(Artifact.objects.filter(pk=artifact.pk).values()) == before
    assert not ArtifactSemanticQuery.objects.filter(artifact=artifact).exists()

    payload = response.json()
    if endpoint == "data":
        assert [q["name"] for q in payload["semantic_queries"]] == ["q.visits_by_day"]
        entries = payload["semantic_query_manifest"]["entries"]
        assert [e["key"] for e in entries] == ["q.visits_by_day"]
    elif endpoint == "query-data":
        assert [(q["name"], q["rows"]) for q in payload["queries"]] == [("q.visits_by_day", [[3]])]
        assert payload["semantic_query_manifest"]["entries"][0]["key"] == "q.visits_by_day"
    else:
        assert payload == {"queryable": True, "status": "ready"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("granularity", [["day"], {"unit": "day"}], ids=["list", "object"])
@pytest.mark.parametrize("endpoint", ["data", "query-data"])
def test_manifest_less_story_with_non_string_granularity_is_an_invalid_query(
    workspace, member_user, endpoint, granularity
):
    doc = graph_doc()
    doc["blocks"][1]["config"]["queries"]["visits_by_day"]["granularity"] = granularity
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Legacy",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": doc},
    )
    client = Client()
    client.force_login(member_user)

    response = client.get(f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/{endpoint}/")

    assert response.status_code == 200, response.content
    [entry] = response.json()["semantic_query_manifest"]["entries"]
    assert entry["key"] == "q.visits_by_day"
    assert entry["validation_status"] == "invalid"
    assert "query_granularity" in [item["kind"] for item in entry["unresolved_references"]]


def _set_block_type(doc, value):
    doc["blocks"][2]["type"] = value


def _set_date_default(doc, value):
    doc["blocks"][0]["config"]["default"] = value


def _set_default_comparison(doc, value):
    doc["blocks"].insert(
        0, {"id": "period", "type": "period_selector", "config": {"default_comparison": value}}
    )


def _set_chart_type(doc, value):
    doc["blocks"][2]["config"]["chart_type"] = value


def _set_series_color(doc, value):
    doc["blocks"][2]["config"]["series"] = [{"key": "visits_count", "color": value}]


def _set_style_value(doc, value):
    doc["blocks"][2]["config"]["style"] = {"legend": value}


def _set_recharts_type(doc, value):
    config = doc["blocks"][2]["config"]
    for key in ("chart_type", "x_key", "series"):
        config.pop(key)
    config["recharts"] = {"type": value, "props": {"stroke": "var(--chart-1)"}}


def _set_granularity(doc, value):
    doc["blocks"][1]["config"]["queries"]["visits_by_day"]["granularity"] = value


WRONG_TYPED_FIELDS = {
    "block_type": (_set_block_type, "unknown_block_type"),
    "date_default": (_set_date_default, "date_preset"),
    "default_comparison": (_set_default_comparison, "date_comparison"),
    "chart_type": (_set_chart_type, "graph_chart_type"),
    "series_color": (_set_series_color, "recharts_color"),
    "style_value": (_set_style_value, "config_value"),
    "recharts_type": (_set_recharts_type, "recharts_type"),
    "granularity": (_set_granularity, "query_granularity"),
}
# query-data resolves date bindings first and rejects these with its handled 400, as it
# would an unsupported preset string.
UNRESOLVABLE_DATE_BINDING_FIELDS = {"block_type", "date_default"}
WRONG_TYPED_VALUES = pytest.mark.parametrize(
    "value", [["line"], {"kind": "line"}], ids=["list", "object"]
)


@WRONG_TYPED_VALUES
@pytest.mark.parametrize("field", WRONG_TYPED_FIELDS)
def test_validate_doc_reports_a_wrong_typed_enum_value(field, value):
    doc = graph_doc()
    mutate, code = WRONG_TYPED_FIELDS[field]
    mutate(doc, value)

    assert code in [item["code"] for item in validate_doc(doc)]


@pytest.mark.django_db(transaction=True)
@WRONG_TYPED_VALUES
@pytest.mark.parametrize("field", WRONG_TYPED_FIELDS)
@pytest.mark.parametrize("endpoint", ["data", "query-data"])
def test_stored_story_with_a_wrong_typed_enum_value_still_reads(
    workspace, member_user, endpoint, field, value
):
    doc = graph_doc()
    WRONG_TYPED_FIELDS[field][0](doc, value)
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Legacy",
        artifact_type=ArtifactType.STORY,
        data={"story_doc": doc},
    )
    client = Client()
    client.force_login(member_user)

    async def execute(_workspace, query, **kwargs):
        return {"columns": ["visits_count"], "rows": [[3]], "row_count": 1, "semantic_query": query}

    with (
        patch("apps.artifacts.services.query_batch.run_semantic_query", side_effect=execute),
        patch(
            "apps.artifacts.views.current_artifact_data_state",
            new=AsyncMock(return_value={"queryable": True, "status": "ready"}),
        ),
    ):
        response = client.get(f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/{endpoint}/")

    if endpoint == "query-data" and field in UNRESOLVABLE_DATE_BINDING_FIELDS:
        assert response.status_code == 400, response.content
        assert response.json()["error"].startswith("Invalid artifact date context")
    else:
        assert response.status_code == 200, response.content


@pytest.mark.django_db(transaction=True)
def test_recovery_post_persists_the_missing_manifest(workspace, member_user):
    artifact = _manifest_less_story(workspace, member_user)
    client = Client()
    client.force_login(member_user)

    with patch(
        "apps.artifacts.services.recovery.current_artifact_data_state",
        new=AsyncMock(return_value={"status": "ready", "queryable": True}),
    ):
        response = client.post(f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/recovery/")

    assert response.status_code == 200, response.content
    artifact.refresh_from_db()
    assert [q["name"] for q in artifact.semantic_queries] == ["q.visits_by_day"]
    assert list(
        ArtifactSemanticQuery.objects.filter(artifact=artifact).values_list("query_key", flat=True)
    ) == ["q.visits_by_day"]


@pytest.mark.django_db(transaction=True)
def test_recovery_post_survives_a_delete_racing_the_backfill(workspace, member_user):
    artifact = _manifest_less_story(workspace, member_user)
    client = Client()
    client.force_login(member_user)

    def delete_then_backfill(resolved):
        Artifact.objects.get(pk=resolved.pk).soft_delete(member_user)
        backfill_missing_semantic_query_manifest(resolved)

    with (
        patch(
            "apps.artifacts.services.recovery.backfill_missing_semantic_query_manifest",
            new=delete_then_backfill,
        ),
        patch(
            "apps.artifacts.services.recovery.current_artifact_data_state",
            new=AsyncMock(return_value={"status": "ready", "queryable": True}),
        ),
    ):
        response = client.post(f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/recovery/")

    assert response.status_code == 200, response.content
    assert not ArtifactSemanticQuery.objects.filter(artifact=artifact).exists()


@pytest.mark.django_db
def test_backfill_builds_from_the_locked_row_and_reports_whether_it_wrote(workspace, member_user):
    artifact = _manifest_less_story(workspace, member_user)
    stale = Artifact.objects.get(pk=artifact.pk)
    stale.data = {}

    assert backfill_missing_semantic_query_manifest(stale) is True
    assert [q["name"] for q in stale.semantic_queries] == ["q.visits_by_day"]
    assert backfill_missing_semantic_query_manifest(stale) is False

    deleted = _manifest_less_story(workspace, member_user)
    Artifact.objects.get(pk=deleted.pk).soft_delete(member_user)
    assert backfill_missing_semantic_query_manifest(deleted) is False


@pytest.mark.django_db
def test_backfill_command_dry_run_writes_nothing(workspace, member_user):
    artifact = _manifest_less_story(workspace, member_user)
    out = StringIO()

    call_command("backfill_story_manifests", stdout=out)

    assert f"Would backfill story {artifact.pk}" in out.getvalue()
    artifact.refresh_from_db()
    assert artifact.semantic_queries == []
    assert artifact.semantic_query_manifest == {}
    assert not ArtifactSemanticQuery.objects.filter(artifact=artifact).exists()


@pytest.mark.django_db
def test_backfill_command_apply_persists_legacy_manifests_once(workspace, member_user):
    legacy = _manifest_less_story(workspace, member_user)
    deleted = _manifest_less_story(workspace, member_user)
    deleted.soft_delete(member_user)
    react = Artifact.objects.create(
        workspace=workspace, created_by=member_user, title="Chart", artifact_type=ArtifactType.REACT
    )

    applied = StringIO()
    call_command("backfill_story_manifests", "--apply", stdout=applied)

    assert "Backfilled 1 stories (1 now show live data)" in applied.getvalue()
    legacy.refresh_from_db()
    assert [q["name"] for q in legacy.semantic_queries] == ["q.visits_by_day"]
    assert list(
        ArtifactSemanticQuery.objects.filter(artifact=legacy).values_list("query_key", flat=True)
    ) == ["q.visits_by_day"]
    for untouched in (deleted, react):
        stored = Artifact.all_objects.get(pk=untouched.pk)
        assert (stored.semantic_queries, stored.semantic_query_manifest) == ([], {})

    rerun = StringIO()
    call_command("backfill_story_manifests", "--apply", stdout=rerun)
    assert (
        "Backfilled 0 stories (0 now show live data), 0 already done or deleted, 0 failed."
        in rerun.getvalue()
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("persist", "expected_inserts"),
    [
        (sync_artifact_semantic_query_manifest, 2),
        # The loser re-checks under the lock, finds the winner's manifest, and skips.
        (backfill_missing_semantic_query_manifest, 1),
    ],
)
def test_concurrent_manifest_persistence_does_not_collide(
    workspace, member_user, persist, expected_inserts
):
    artifact = _manifest_less_story(workspace, member_user)
    # Holds each writer between its DELETE and its INSERT until the other arrives: the
    # interleaving that collides on unique_artifact_semantic_query_key. While writes
    # are serialised the other writer can't arrive, so the wait times out instead.
    both_deleted = threading.Barrier(2, timeout=2)
    bulk_create = ArtifactSemanticQuery.objects.bulk_create
    inserts = []
    errors = []

    def rendezvous_then_insert(records, *args, **kwargs):
        inserts.append(len(records))
        try:
            both_deleted.wait()
        except threading.BrokenBarrierError:
            pass
        return bulk_create(records, *args, **kwargs)

    def writer():
        try:
            persist(Artifact.objects.get(pk=artifact.pk))
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    with patch.object(
        ArtifactSemanticQuery.objects, "bulk_create", side_effect=rendezvous_then_insert
    ):
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    # A hung writer would otherwise surface as a misleading insert-count failure.
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(inserts) == expected_inserts
    assert list(
        ArtifactSemanticQuery.objects.filter(artifact=artifact).values_list("query_key", flat=True)
    ) == ["q.visits_by_day"]


@pytest.mark.parametrize(
    "series",
    [
        "visits_segment",
        None,
        {},
        42,
        [],
        [""],
        [None],
        [42],
        [{}],
        [{"data_key": 42}],
        [{"data_key": "visits_count", "label": 42}],
    ],
)
def test_graph_doc_rejects_invalid_series_with_long_format_guidance(series):
    doc = graph_doc()
    doc["blocks"][2]["config"]["series"] = series
    diagnostics = validate_doc(doc)
    errors = [item for item in diagnostics if item.get("code") == "graph_series"]
    assert errors
    assert errors[0]["severity"] == "error"
    assert "series_by" in errors[0]["message"]


@pytest.mark.parametrize(
    "series",
    [
        ["visits_count"],
        [{"data_key": "visits_count", "label": "Visits"}],
        [{"y_key": "visits_count", "name": "Visits"}],
        [{"key": "visits_count"}],
        [{"data_key": "", "y_key": "visits_count"}],
        [{"data_key": "", "y_key": "", "key": "visits_count"}],
    ],
)
def test_graph_doc_accepts_legacy_series_forms(series):
    doc = graph_doc()
    doc["blocks"][2]["config"]["series"] = series
    assert validate_doc(doc) == []


@pytest.mark.parametrize(
    "series", [["absent"], [{"data_key": "absent"}], [{"y_key": "absent"}], [{"key": "absent"}]]
)
def test_graph_doc_checks_every_series_key_against_query(series):
    doc = graph_doc()
    doc["blocks"][2]["config"]["series"] = series
    assert any(item.get("code") == "missing_result_key:absent" for item in validate_doc(doc))


@pytest.mark.parametrize("chart_type", ["bar", "area", "line"])
def test_graph_doc_accepts_dimension_series(chart_type):
    doc = graph_doc()
    doc["blocks"][1]["config"]["queries"]["visits_by_day"]["dimensions"] = ["visits.segment"]
    doc["blocks"][2]["config"] = {
        "chart_type": chart_type,
        "x_key": "date",
        "y_key": "visits_count",
        "series_by": "visits_segment",
        "stacked": chart_type != "line",
    }
    assert validate_doc(doc) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"series_by": None},
        {"series_by": []},
        {"series_by": ""},
        {"series": ["visits_count"]},
        {"y_key": None},
        {"x_key": None},
        {"chart_type": "pie"},
        {"chart_type": "donut"},
        {"recharts": {"type": "BarChart"}},
    ],
)
def test_graph_doc_rejects_invalid_dimension_series(changes):
    doc = graph_doc()
    doc["blocks"][2]["config"] = {
        "chart_type": "bar",
        "x_key": "date",
        "y_key": "visits_count",
        "series_by": "visits_segment",
        **changes,
    }
    assert any(item.get("code") == "graph_series_by" for item in validate_doc(doc))


def test_graph_doc_checks_series_by_against_bound_query():
    doc = graph_doc()
    doc["blocks"][2]["config"] = {
        "chart_type": "bar",
        "x_key": "date",
        "y_key": "visits_count",
        "series_by": "absent",
    }
    assert any(item.get("code") == "missing_result_key:absent" for item in validate_doc(doc))


def test_graph_doc_identifies_invalid_series_entry():
    doc = graph_doc()
    doc["blocks"][2]["config"]["series"] = ["visits_count", {}]
    errors = [item for item in validate_doc(doc) if item.get("code") == "graph_series"]
    assert len(errors) == 1
    assert "series[1]" in errors[0]["message"]
