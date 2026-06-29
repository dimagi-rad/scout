"""Tests for ArtifactQueryDataView — live query execution via MCP service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
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
        artifact_type=ArtifactType.REACT,
        code="export default function() { return <div/> }",
        conversation_id="thread-1",
        source_queries=[
            {"name": "submissions", "sql": "SELECT count(*) as total FROM forms"},
            {"name": "daily", "sql": "SELECT date, count(*) FROM forms GROUP BY date"},
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


FAKE_CTX = MagicMock()
FAKE_CTX.schema_name = "test_domain"

MOCK_SUBMISSIONS_RESULT = {
    "columns": ["total"],
    "rows": [[99]],
    "row_count": 1,
    "truncated": False,
    "sql_executed": "SELECT count(*) as total FROM forms LIMIT 500",
    "tables_accessed": ["forms"],
}

MOCK_DAILY_RESULT = {
    "columns": ["date", "count"],
    "rows": [["2024-01-01", 10], ["2024-01-02", 20]],
    "row_count": 2,
    "truncated": False,
    "sql_executed": "SELECT date, count(*) FROM forms GROUP BY date LIMIT 500",
    "tables_accessed": ["forms"],
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_returns_query_results_for_live_artifact(live_artifact, member_client, membership):
    """Happy path: queries are executed and results returned with correct shape."""
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    with (
        patch(
            "apps.artifacts.views.load_workspace_context",
            new=AsyncMock(return_value=FAKE_CTX),
        ),
        patch(
            "apps.artifacts.views.execute_query",
            new=AsyncMock(side_effect=[MOCK_SUBMISSIONS_RESULT, MOCK_DAILY_RESULT]),
        ),
    ):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["queries"]) == 2
    assert data["queries"][0]["name"] == "submissions"
    assert data["queries"][0]["columns"] == ["total"]
    assert data["queries"][0]["rows"] == [[99]]
    assert data["queries"][1]["name"] == "daily"
    assert "error" not in data["queries"][0]
    assert data["static_data"] == {}


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
        source_queries=[{"name": "q", "sql": "SELECT 1"}],
    )
    url = f"/api/workspaces/{membership.id}/artifacts/{artifact.id}/query-data/"
    response = await member_client.get(url)
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_tenant_context_error_returns_error_query(live_artifact, member_client, membership):
    """If load_workspace_context fails (no schema), return error response."""
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    with patch(
        "apps.artifacts.views.load_workspace_context",
        new=AsyncMock(side_effect=ValueError("No active schema")),
    ):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert all("error" in q for q in data["queries"])


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_individual_query_failure_continues(live_artifact, member_client, membership):
    """A failed query includes an error entry; other queries still execute."""
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    error_result = {"success": False, "error": {"code": "QUERY_TIMEOUT", "message": "Timed out"}}

    with (
        patch(
            "apps.artifacts.views.load_workspace_context",
            new=AsyncMock(return_value=FAKE_CTX),
        ),
        patch(
            "apps.artifacts.views.execute_query",
            new=AsyncMock(side_effect=[error_result, MOCK_DAILY_RESULT]),
        ),
    ):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["queries"]) == 2
    assert "error" in data["queries"][0]
    assert data["queries"][0]["name"] == "submissions"
    assert data["queries"][1]["name"] == "daily"
    assert "error" not in data["queries"][1]


# parallel execution + result caching (arch #254, finding 09#9)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_source_queries_run_concurrently(live_artifact, member_client, membership):
    """The source queries execute concurrently, not strictly serially (09#9).

    Each execute_query waits on a shared barrier: if they ran serially the
    second would never start until the first returned, and the barrier (which
    needs both) would deadlock. Completing proves they overlap.
    """
    cache.clear()
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    started = asyncio.Event()
    in_flight = {"n": 0, "max": 0}

    async def slow_execute(ctx, sql):
        in_flight["n"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["n"])
        # Yield so the other coroutine can start before we return.
        await asyncio.sleep(0.05)
        in_flight["n"] -= 1
        started.set()
        if "count(*) as total" in sql:
            return MOCK_SUBMISSIONS_RESULT
        return MOCK_DAILY_RESULT

    with (
        patch(
            "apps.artifacts.views.load_workspace_context",
            new=AsyncMock(return_value=FAKE_CTX),
        ),
        patch("apps.artifacts.views.execute_query", new=slow_execute),
    ):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    # Both queries overlapped at least once → ran concurrently.
    assert in_flight["max"] >= 2
    # Results are still assembled in source_queries order.
    assert [q["name"] for q in data["queries"]] == ["submissions", "daily"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_query_results_cached_across_opens(live_artifact, member_client, membership):
    """A second open within the cache TTL must not re-execute the source
    queries (09#9 — live artifacts re-ran every query on every open)."""
    cache.clear()
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    exec_mock = AsyncMock(side_effect=[MOCK_SUBMISSIONS_RESULT, MOCK_DAILY_RESULT])
    with (
        patch(
            "apps.artifacts.views.load_workspace_context",
            new=AsyncMock(return_value=FAKE_CTX),
        ),
        patch("apps.artifacts.views.execute_query", new=exec_mock),
    ):
        first = await member_client.get(url)
        calls_after_first = exec_mock.await_count
        second = await member_client.get(url)
        calls_after_second = exec_mock.await_count

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["queries"] == second.json()["queries"]
    # No re-execution on the cached second open.
    assert calls_after_second == calls_after_first
