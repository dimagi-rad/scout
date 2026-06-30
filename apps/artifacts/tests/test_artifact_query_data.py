"""Tests for ArtifactQueryDataView semantic live-data execution."""

from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient

from apps.artifacts.models import Artifact, ArtifactType
from apps.users.models import TenantMembership, User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant


@pytest.fixture
def workspace(db):
    from apps.users.models import Tenant

    tenant = Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )
    ws = Workspace.objects.create(name="Test Domain")
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def member_user(db, workspace):
    user = User.objects.create_user(email="member@example.com", password="pass")
    TenantMembership.objects.create(user=user, tenant=workspace.tenant)
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.MANAGE)
    return user


@pytest.fixture
def membership(db, workspace, member_user):
    """Returns the workspace (used as the URL parameter)."""
    return workspace


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="pass")


@pytest.fixture
def other_workspace(db):
    from apps.users.models import Tenant

    tenant = Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other Domain"
    )
    ws = Workspace.objects.create(name="Other Domain")
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def other_membership(db, other_workspace, other_user):
    """Returns the other workspace (used as the URL parameter)."""
    TenantMembership.objects.create(user=other_user, tenant=other_workspace.tenant)
    WorkspaceMembership.objects.create(
        workspace=other_workspace, user=other_user, role=WorkspaceRole.MANAGE
    )
    return other_workspace


def _make_auth_client(user):
    """Return an AsyncClient logged in as user, with update_last_login signal disconnected."""
    client = AsyncClient()
    user_logged_in.disconnect(update_last_login)
    try:
        client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)
    return client


@pytest.fixture
def member_client(member_user):
    return _make_auth_client(member_user)


@pytest.fixture
def other_client(other_user):
    return _make_auth_client(other_user)


@pytest.fixture
def live_artifact(db, workspace, member_user):
    return Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Live Chart",
        artifact_type=ArtifactType.STORY,
        code="",
        conversation_id="thread-1",
        data={"story_doc": {"version": 1, "blocks": []}},
        semantic_queries=[
            {"name": "submissions", "measures": ["visits.count"]},
            {
                "name": "daily",
                "measures": ["visits.count"],
                "time_dimension": "visits.visit_date",
                "granularity": "day",
            },
        ],
    )


@pytest.fixture
def legacy_sql_artifact(db, workspace, member_user):
    return Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Legacy SQL Chart",
        artifact_type=ArtifactType.REACT,
        code="export default function() { return <div/> }",
        conversation_id="thread-legacy",
        source_queries=[
            {"name": "submissions", "sql": "SELECT count(*) as total FROM forms"},
        ],
    )


@pytest.fixture
def static_artifact(db, workspace, member_user):
    return Artifact.objects.create(
        workspace=workspace,
        created_by=member_user,
        title="Static Chart",
        artifact_type=ArtifactType.REACT,
        code="export default function() { return <div/> }",
        conversation_id="thread-2",
        source_queries=[],
        data={"total": 42},
    )


MOCK_SUBMISSIONS_RESULT = {
    "columns": ["total"],
    "rows": [[99]],
    "row_count": 1,
    "truncated": False,
    "semantic_query": {"measures": ["visits.count"], "limit": 100},
    "members": ["visits.count"],
}

MOCK_DAILY_RESULT = {
    "columns": ["date", "count"],
    "rows": [["2024-01-01", 10], ["2024-01-02", 20]],
    "row_count": 2,
    "truncated": False,
    "semantic_query": {
        "measures": ["visits.count"],
        "time_dimension": "visits.visit_date",
        "granularity": "day",
        "limit": 100,
    },
    "members": ["visits.visit_date", "visits.count"],
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_returns_query_results_for_live_artifact(live_artifact, member_client, membership):
    """Happy path: semantic queries are executed and returned with correct shape."""
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(side_effect=[MOCK_SUBMISSIONS_RESULT, MOCK_DAILY_RESULT]),
    ):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["queries"]) == 2
    assert data["queries"][0]["name"] == "submissions"
    assert data["queries"][0]["columns"] == ["total"]
    assert data["queries"][0]["rows"] == [[99]]
    assert "semantic_query" in data["queries"][0]
    assert "sql" not in data["queries"][0]
    assert data["queries"][1]["name"] == "daily"
    assert "error" not in data["queries"][0]
    assert data["static_data"] == {"story_doc": {"version": 1, "blocks": []}}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_returns_empty_queries_for_static_artifact(
    static_artifact, member_client, membership
):
    """Artifacts with no source_queries return empty queries list."""
    url = f"/api/workspaces/{membership.id}/artifacts/{static_artifact.id}/query-data/"
    response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["queries"] == []
    assert data["static_data"] == {"total": 42}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_unauthenticated_returns_401(live_artifact, membership):
    """Unauthenticated request returns 401."""
    client = AsyncClient()
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"
    response = await client.get(url)
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_non_member_returns_404(live_artifact, other_client, other_membership):
    """User from a different workspace cannot access artifacts scoped to this workspace."""
    url = f"/api/workspaces/{other_membership.id}/artifacts/{live_artifact.id}/query-data/"
    response = await other_client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_no_workspace_returns_404(member_user, member_client, membership):
    """Artifact with no workspace is not found in the scoped workspace."""
    artifact = await Artifact.objects.acreate(
        workspace=None,
        created_by=member_user,
        title="Orphan",
        artifact_type=ArtifactType.REACT,
        code="x",
        conversation_id="t",
        semantic_queries=[{"name": "q", "measures": ["visits.count"]}],
    )
    url = f"/api/workspaces/{membership.id}/artifacts/{artifact.id}/query-data/"
    response = await member_client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_legacy_sql_source_queries_are_disabled(
    legacy_sql_artifact, member_client, membership
):
    """Legacy SQL-backed artifacts do not execute source_queries."""
    url = f"/api/workspaces/{membership.id}/artifacts/{legacy_sql_artifact.id}/query-data/"
    response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["queries"] == [
        {
            "name": "submissions",
            "error": (
                "Legacy SQL-backed artifact queries are disabled. "
                "Recreate this artifact with semantic_queries."
            ),
        }
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_individual_query_failure_continues(live_artifact, member_client, membership):
    """A failed semantic query includes an error entry; other queries still execute."""
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    error_result = {"success": False, "error": {"code": "QUERY_TIMEOUT", "message": "Timed out"}}

    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(side_effect=[error_result, MOCK_DAILY_RESULT]),
    ):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["queries"]) == 2
    assert "error" in data["queries"][0]
    assert data["queries"][0]["name"] == "submissions"
    assert data["queries"][1]["name"] == "daily"
    assert "error" not in data["queries"][1]
