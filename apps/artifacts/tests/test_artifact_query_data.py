"""Tests for ArtifactQueryDataView — live query execution via MCP service."""

import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from django.test import AsyncClient
from freezegun import freeze_time

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.artifacts.views import _artifact_query_cache_key
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from tests.tenant_access import usable_connection
from tests.test_artifact_date_resolution import CONTEXT, story


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_query_inspector_resolves_controls_and_separates_filter_cache(
    member_client, workspace, live_artifact
):
    live_artifact.data = {"story_doc": story()}
    await live_artifact.asave(update_fields=["data"])
    await cache.aclear()
    endpoint = f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"

    async def execute(_workspace, query, **kwargs):
        bounds = query["filters"][0]["values"]
        return {
            "columns": ["sessions.count"],
            "rows": [[18 if bounds[0] == "2026-08-18" else 5]],
            "row_count": 1,
            "semantic_query": query,
        }

    with patch("apps.artifacts.views.run_semantic_query", side_effect=execute) as run:
        initial = await member_client.post(endpoint, CONTEXT, content_type="application/json")
        assert initial.status_code == 200
        assert initial.json()["queries"][0]["rows"] == [[18]]
        selected = {**CONTEXT, "sources": {"range": {"start": "2026-09-10", "end": "2026-09-16"}}}
        changed = await member_client.post(endpoint, selected, content_type="application/json")
        assert changed.json()["queries"][0]["rows"] == [[5]]
        assert run.await_count == 2
        repeated = await member_client.post(endpoint, selected, content_type="application/json")
        assert repeated.json()["queries"][0]["rows"] == [[5]]
        assert run.await_count == 2
        same_bounds_new_clock = await member_client.post(
            endpoint, {**selected, "as_of": "2026-09-16T14:00:00Z"}, content_type="application/json"
        )
        assert same_bounds_new_clock.status_code == 200
        assert run.await_count == 2
        body = same_bounds_new_clock.json()
        assert body["query_context"]["as_of"].startswith("2026-09-16T14:00:00")
        assert body["queries"][0]["semantic_query"]["query_context"] == {
            "timezone": CONTEXT["timezone"]
        }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inspector_never_executes_a_manifest_rejected_query(
    member_client, workspace, live_artifact
):
    doc = story()
    doc["blocks"][1]["config"]["queries"]["sessions"]["dateRange"] = ["2026-09-01", "2026-09-10"]
    live_artifact.data = {"story_doc": doc}
    await live_artifact.asave(update_fields=["data"])
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as execute:
        response = await member_client.post(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/",
            CONTEXT,
            content_type="application/json",
        )
    assert response.status_code == 400
    execute.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inspector_bounds_concurrent_queries(member_client, workspace, live_artifact):
    live_artifact.semantic_queries = [
        {"name": f"q{i}", "measures": ["visits.count"]} for i in range(9)
    ]
    await live_artifact.asave(update_fields=["semantic_queries"])
    active = 0
    peak = 0

    async def execute(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {"columns": ["visits.count"], "rows": [[1]], "row_count": 1}

    with patch("apps.artifacts.views.run_semantic_query", side_effect=execute):
        response = await member_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
        )
    assert response.status_code == 200
    assert len(response.json()["queries"]) == 9
    assert 2 <= peak <= 4


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inspector_invalid_binding_does_not_fall_back_to_all_time(
    member_client, workspace, live_artifact, caplog
):
    doc = story()
    doc["blocks"][1]["inputs"]["date_range"] = {"$ref": "removed.value"}
    live_artifact.data = {"story_doc": doc}
    await live_artifact.asave(update_fields=["data"])
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as run:
        response = await member_client.post(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/",
            CONTEXT,
            content_type="application/json",
        )
    assert response.status_code == 400
    assert response.json() == {
        "error": "Invalid artifact date context. Check the dates, timezone, and date-control bindings."
    }
    run.assert_not_awaited()
    assert str(live_artifact.id) in caplog.text
    assert "Unresolved date binding: removed.value" in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inspector_and_runtime_check_execute_same_default_periods(
    member_client, workspace, live_artifact
):
    live_artifact.data = {"story_doc": story(compare=True)}
    await live_artifact.asave(update_fields=["data"])
    result = {"columns": ["sessions.count"], "rows": [[18]], "row_count": 1}
    with (
        freeze_time("2026-09-16T13:00:00Z"),
        patch(
            "apps.artifacts.views.run_semantic_query", new=AsyncMock(return_value=result)
        ) as inspect,
        patch(
            "apps.artifacts.services.graph_runtime.run_semantic_query",
            new=AsyncMock(return_value=result),
        ) as check,
    ):
        response = await member_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
        )
        checked = await check_graph_artifact(live_artifact)
    assert response.status_code == 200
    assert checked["success"] is True
    assert inspect.await_count == check.await_count == 2
    for inspected, validated in zip(inspect.await_args_list, check.await_args_list, strict=True):
        # Runtime checks cap row count; date ranges/timezones are identical.
        actual = copy.deepcopy(validated.args[1])
        actual.pop("limit", None)
        assert inspected.args[1] == actual


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_narrative_only_story_does_not_claim_a_query_date_context(
    member_client, workspace, live_artifact
):
    live_artifact.data = {
        "story_doc": {
            "schema_version": 1,
            "blocks": [{"id": "intro", "type": "markdown", "config": {"text": "Overview"}}],
        }
    }
    await live_artifact.asave(update_fields=["data"])
    result = {"columns": ["visits.count"], "rows": [[18]], "row_count": 1}
    with patch("apps.artifacts.views.run_semantic_query", new=AsyncMock(return_value=result)):
        response = await member_client.post(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/",
            CONTEXT,
            content_type="application/json",
        )
    assert response.status_code == 200
    assert response.json()["queries"]
    assert response.json()["query_context"] is None


@pytest.fixture(autouse=True)
def query_surface_ready():
    """These query execution tests isolate result handling from recovery state."""
    with patch(
        "apps.artifacts.views.artifact_data_state",
        new=AsyncMock(
            return_value={
                "status": "ready",
                "queryable": True,
                "message": "Artifact data is ready.",
            }
        ),
    ):
        yield


@pytest.fixture
def workspace(db):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )
    ws = Workspace.objects.create(name="Test Domain")
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def member_user(db, workspace):
    user = User.objects.create_user(email="member@example.com", password="pass")
    TenantMembership.objects.create(
        user=user,
        tenant=workspace.tenant,
        connection=usable_connection(user, workspace.tenant.provider),
    )
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
    tenant = Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other Domain"
    )
    ws = Workspace.objects.create(name="Other Domain")
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def other_membership(db, other_workspace, other_user):
    """Returns the other workspace (used as the URL parameter)."""
    TenantMembership.objects.create(
        user=other_user,
        tenant=other_workspace.tenant,
        connection=usable_connection(other_user, other_workspace.tenant.provider),
    )
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


# parallel execution + result caching (arch #254, finding 09#9)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_semantic_queries_run_concurrently(live_artifact, member_client, membership):
    """The semantic queries execute concurrently, not strictly serially (09#9).

    Each execute_query waits on a shared barrier: if they ran serially the
    second would never start until the first returned, and the barrier (which
    needs both) would deadlock. Completing proves they overlap.
    """
    cache.clear()
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    started = asyncio.Event()
    in_flight = {"n": 0, "max": 0}

    async def slow_execute(workspace, query_spec, *, user_id):
        in_flight["n"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["n"])
        # Yield so the other coroutine can start before we return.
        await asyncio.sleep(0.05)
        in_flight["n"] -= 1
        started.set()
        return MOCK_DAILY_RESULT if query_spec.get("time_dimension") else MOCK_SUBMISSIONS_RESULT

    with patch("apps.artifacts.views.run_semantic_query", new=slow_execute):
        response = await member_client.get(url)

    assert response.status_code == 200
    data = response.json()
    # Both queries overlapped at least once → ran concurrently.
    assert in_flight["max"] >= 2
    # Results are still assembled in semantic_queries order.
    assert [q["name"] for q in data["queries"]] == ["submissions", "daily"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_query_results_cached_across_opens(live_artifact, member_client, membership):
    """A second open within the cache TTL must not re-execute the semantic
    queries (09#9 — live artifacts re-ran every query on every open)."""
    cache.clear()
    url = f"/api/workspaces/{membership.id}/artifacts/{live_artifact.id}/query-data/"

    exec_mock = AsyncMock(side_effect=[MOCK_SUBMISSIONS_RESULT, MOCK_DAILY_RESULT])
    with patch("apps.artifacts.views.run_semantic_query", new=exec_mock):
        first = await member_client.get(url)
        calls_after_first = exec_mock.await_count
        second = await member_client.get(url)
        calls_after_second = exec_mock.await_count

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["queries"] == second.json()["queries"]
    # No re-execution on the cached second open.
    assert calls_after_second == calls_after_first


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_malformed_stored_query_does_not_crash_inspector(
    live_artifact, member_client, workspace
):
    live_artifact.semantic_queries = [
        None,
        {"name": "valid", "measures": ["visits.count"], "query_context": {}},
    ]
    await live_artifact.asave(update_fields=["semantic_queries"])
    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(return_value=MOCK_SUBMISSIONS_RESULT),
    ) as run:
        response = await member_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
        )
    assert response.status_code == 200
    assert response.json()["queries"][0]["error"] == "Semantic query must be an object"
    assert response.json()["queries"][1]["rows"] == MOCK_SUBMISSIONS_RESULT["rows"]
    run.assert_awaited_once()


def test_cache_context_without_timezone_uses_default(live_artifact, settings):
    first = _artifact_query_cache_key(live_artifact, resolved_queries=[{"query_context": {}}])
    second = _artifact_query_cache_key(
        live_artifact, resolved_queries=[{"query_context": {"timezone": settings.TIME_ZONE}}]
    )
    assert first == second


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_narrative_document_preserves_explicit_stored_queries(
    live_artifact, member_client, workspace
):
    live_artifact.data = {
        "story_doc": {
            "schema_version": 1,
            "blocks": [
                {"id": "summary", "type": "tldr", "config": {"text": "Visits overview"}},
            ],
        }
    }
    await live_artifact.asave(update_fields=["data"])
    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(return_value=MOCK_SUBMISSIONS_RESULT),
    ) as run:
        response = await member_client.post(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/",
            CONTEXT,
            content_type="application/json",
        )
    assert response.status_code == 200
    assert [query["name"] for query in response.json()["queries"]] == ["submissions", "daily"]
    assert run.await_count == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [(b"", 200), (b"{", 400), (b"\xff", 400)])
async def test_inspector_validates_json_separately_from_dates(
    live_artifact, member_client, workspace, body, status
):
    live_artifact.data = {"story_doc": story()}
    await live_artifact.asave(update_fields=["data"])
    await cache.aclear()
    with patch(
        "apps.artifacts.views.run_semantic_query",
        new=AsyncMock(return_value=MOCK_SUBMISSIONS_RESULT),
    ) as run:
        response = await member_client.post(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/",
            body,
            content_type="application/json",
        )
    assert response.status_code == status
    if status == 400:
        assert response.json()["error"] == "Request body must be valid UTF-8 JSON."
        run.assert_not_awaited()
    else:
        run.assert_awaited_once()
