from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient

from apps.agents.tools.artifact_graph_tool import create_artifact_graph_tools
from apps.agents.tools.artifact_tool import create_artifact_tools
from apps.artifacts.models import Artifact, ArtifactSemanticQuery, ArtifactType
from apps.artifacts.services.graph_doc import GraphDocError, apply_ops, validate_doc
from apps.artifacts.services.graph_manifest import sync_artifact_semantic_query_manifest
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant


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
    TenantMembership.objects.create(user=user, tenant=workspace.tenant)
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

    diagnostics = [
        item for item in validate_doc(doc) if item.get("code") == "unknown_config_key"
    ]

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
async def test_graph_manager_creates_story_and_generic_tool_rejects_story(workspace, member_user):
    graph_tool = next(
        item for item in create_artifact_graph_tools(workspace, member_user, "thread") if item.name == "artifact_write"
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

    dependencies_tool = next(
        item
        for item in create_artifact_graph_tools(workspace, member_user, "thread")
        if item.name == "get_artifact_semantic_queries"
    )
    dependencies = await dependencies_tool.ainvoke({"artifact_id": result["artifact"]["id"], "limit": 1, "offset": 0})
    assert dependencies["status"] == "ok"
    assert dependencies["pagination"]["has_more"] is False
    assert dependencies["semantic_queries"][0]["query_key"] == "q.visits_by_day"

    create_tool = next(item for item in create_artifact_tools(workspace, member_user, "thread") if item.name == "create_artifact")
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
