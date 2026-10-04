"""Tests for ArtifactQueryDataView — live query execution via MCP service."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth.models import update_last_login
from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from django.db import OperationalError
from django.test import AsyncClient
from freezegun import freeze_time

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.artifacts.views import _artifact_query_cache_key
from apps.semantic.services.query import CAPACITY_EXHAUSTED_CATEGORY
from apps.semantic.services.query_outcomes import query_error, query_readiness_error
from apps.semantic.services.query_readiness import QuerySurfaceReadiness
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
@pytest.mark.parametrize(
    "outcome",
    [
        query_error(
            "CONNECTION_ERROR",
            "Cube is at its database connection limit.",
            category=CAPACITY_EXHAUSTED_CATEGORY,
            retryable=True,
        ),
        OperationalError("FATAL:  sorry, too many clients already"),
    ],
    ids=["cube_pool_full", "database_full"],
)
async def test_a_full_connection_limit_makes_the_panel_retryable(
    member_client, workspace, live_artifact, outcome
):
    if isinstance(outcome, Exception):
        run = AsyncMock(side_effect=outcome)
    else:
        run = AsyncMock(return_value=outcome)
    with (
        patch("apps.artifacts.views.run_semantic_query", new=run),
        patch("apps.common.capacity.sentry_sdk"),
    ):
        response = await member_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
        )
    assert response.status_code == 503
    assert response["Retry-After"]
    assert response.json()["error"] == "busy"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inspector_shares_one_readiness_inspection_across_query_failures(
    member_client, workspace, live_artifact
):
    live_artifact.semantic_queries = [
        {"name": f"q{i}", "measures": ["visits.count"]} for i in range(9)
    ]
    await live_artifact.asave(update_fields=["semantic_queries"])
    await cache.aclear()

    async def execute(query_workspace, query, *, readiness, **kwargs):
        return await query_readiness_error(
            query_workspace,
            query,
            "VALIDATION_ERROR",
            "Serving model unavailable",
            category="invalid_query",
            readiness=readiness,
        )

    with (
        patch("apps.artifacts.views.run_semantic_query", side_effect=execute) as run,
        patch(
            "apps.semantic.services.query_outcomes.query_surface_readiness",
            new=AsyncMock(
                return_value=QuerySurfaceReadiness(
                    {
                        "queryable": False,
                        "status": "needs_semantic_rebuild",
                        "recovery_action": "semantic_rebuild",
                    }
                )
            ),
        ) as inspect,
    ):
        response = await member_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
        )
    assert response.status_code == 200
    assert run.await_count == 9
    inspect.assert_awaited_once()
    assert inspect.await_args.args[1] == [{"measures": ["visits.count"]}] * 9
    assert len(response.json()["queries"]) == 9
    assert all(
        result["error"] == "Serving model unavailable" for result in response.json()["queries"]
    )


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
@pytest.mark.parametrize("compare", [False, True], ids=["date_filter", "comparison"])
async def test_inspector_and_runtime_check_execute_same_default_periods(
    member_client, workspace, live_artifact, compare
):
    live_artifact.data = {"story_doc": story(compare=compare)}
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
    assert inspect.await_count == check.await_count == (2 if compare else 1)
    assert [q["name"] for q in response.json()["queries"]] == [
        q["query_key"] for q in checked["queries"]
    ]
    assert response.json()["query_context"] == checked["query_context"]
    assert response.json()["query_context"]["as_of"].startswith("2026-09-16T13:00:00")
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
    tenant = workspace.tenants.get()
    user = User.objects.create_user(email="member@example.com", password="pass")
    TenantMembership.objects.create(
        user=user,
        tenant=tenant,
        connection=usable_connection(user, tenant.provider),
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
    other_tenant = other_workspace.tenants.get()
    TenantMembership.objects.create(
        user=other_user,
        tenant=other_tenant,
        connection=usable_connection(other_user, other_tenant.provider),
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

    async def slow_execute(workspace, query_spec, *, user_id, readiness):
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


INVALID_JSON = {"truncated": b"{", "non_utf8": b"\xff", "deeply_nested": b"[" * 100_000}
NON_OBJECT = {"array": b"[1]", "string": b'"x"', "number": b"5", "null": b"null"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_state", ["story", "stored_queries", "no_queries", "not_ready"])
@pytest.mark.parametrize(
    "body,error",
    [
        *((body, "Invalid JSON") for body in INVALID_JSON.values()),
        *((body, "Request body must be a JSON object.") for body in NON_OBJECT.values()),
    ],
    ids=[*INVALID_JSON, *NON_OBJECT],
)
async def test_inspector_rejects_a_body_that_is_not_a_json_object(
    live_artifact, member_client, workspace, artifact_state, body, error
):
    if artifact_state == "story":
        live_artifact.data = {"story_doc": story()}
        await live_artifact.asave(update_fields=["data"])
    elif artifact_state == "no_queries":
        live_artifact.semantic_queries = []
        await live_artifact.asave(update_fields=["semantic_queries"])
    data_state = {"status": "stale", "queryable": artifact_state != "not_ready", "message": "x"}
    with (
        patch("apps.artifacts.views.artifact_data_state", new=AsyncMock(return_value=data_state)),
        patch("apps.artifacts.views.run_semantic_query", new=AsyncMock()) as run,
    ):
        response = await member_client.post(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/",
            body,
            content_type="application/json",
        )
    assert response.status_code == 400
    assert response.json() == {"error": error}
    run.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_inspector_treats_an_empty_body_as_no_runtime_context(
    live_artifact, member_client, workspace
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
            b"",
            content_type="application/json",
        )
    assert response.status_code == 200
    run.assert_awaited_once()


# Characterization of the batch contract shared by View Data and runtime checks
# (refactor deep dive finding 6): these pin the raw payloads both adapters emit.

LEGACY_SQL_ERROR = (
    "Legacy SQL-backed artifact queries are disabled. Recreate this artifact with semantic_queries."
)


def test_cache_key_identity_is_pinned(live_artifact):
    """Changing the cache key's inputs invalidates (or wrongly shares) every cached panel."""
    key = _artifact_query_cache_key(
        live_artifact, "rev-1", [{"name": "q", "measures": ["visits.count"]}]
    )
    assert key == f"artifact_qdata:{live_artifact.id}:{live_artifact.version}:c3eda29659cb"
    assert key != _artifact_query_cache_key(
        live_artifact, "rev-2", [{"name": "q", "measures": ["visits.count"]}]
    )


def _json_bytes(payload):
    """JsonResponse's exact serialization, so key order and spacing are pinned too."""
    return json.dumps(payload).encode()


def _batch_execute(results):
    """Answer each query by its first measure so assertions don't depend on scheduling."""

    async def execute(_workspace, query, **kwargs):
        outcome = results[query["measures"][0]]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(query) if callable(outcome) else outcome

    return execute


def _ok_result(query):
    return {
        "columns": ["visits.count"],
        "rows": [[3]],
        "row_count": 1,
        "semantic_query": {**query, "query_context": {"as_of": "x", "timezone": "UTC"}},
        "members": ["visits.count"],
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_view_data_partial_failure_payload_and_no_caching(
    live_artifact, member_client, workspace, caplog
):
    live_artifact.semantic_queries = [
        {"name": "ok", "measures": ["ok.count"]},
        {"name": "failed", "measures": ["failed.count"], "limit": 7},
        {"name": "raised", "measures": ["raised.count"]},
        "not-an-object",
        {"name": "plain_error", "measures": ["plain.count"]},
    ]
    live_artifact.source_queries = [{"name": "legacy", "sql": "SELECT 1"}]
    await live_artifact.asave(update_fields=["semantic_queries", "source_queries"])
    await cache.aclear()
    execute = _batch_execute(
        {
            "ok.count": _ok_result,
            "failed.count": {
                "success": False,
                "error": {"code": "QUERY_TIMEOUT", "message": "Timed out"},
            },
            "raised.count": RuntimeError("cube exploded"),
            "plain.count": {"error": "plain text failure"},
        }
    )
    with patch("apps.artifacts.views.run_semantic_query", side_effect=execute) as run:
        response = await member_client.get(
            f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
        )
    assert response.status_code == 200
    assert run.await_count == 4
    assert response.content == _json_bytes(
        {
            "queries": [
                {
                    "name": "ok",
                    "semantic_query": {
                        "measures": ["ok.count"],
                        "query_context": {"timezone": "UTC"},
                    },
                    "columns": ["visits.count"],
                    "rows": [[3]],
                    "row_count": 1,
                    "truncated": False,
                },
                {
                    "name": "failed",
                    "semantic_query": {"limit": 7, "measures": ["failed.count"]},
                    "error": "Timed out",
                },
                {
                    "name": "raised",
                    "semantic_query": {"measures": ["raised.count"]},
                    "error": "Semantic query failed",
                },
                {"name": "semantic_query_3", "error": "Semantic query must be an object"},
                {
                    "name": "plain_error",
                    "semantic_query": {"measures": ["plain.count"]},
                    "error": "plain text failure",
                },
                {"name": "legacy", "error": LEGACY_SQL_ERROR},
            ],
            "query_context": None,
            "static_data": {"story_doc": {"blocks": [], "version": 1}},
            "semantic_query_manifest": {},
        }
    )
    record = next(r for r in caplog.records if "Artifact query 'raised' failed" in r.getMessage())
    assert record.exc_info is not None
    key = _artifact_query_cache_key(live_artifact, "", live_artifact.semantic_queries)
    assert await cache.aget(key) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_view_data_caches_only_a_fully_successful_batch(
    live_artifact, member_client, workspace
):
    await cache.aclear()
    url = f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
    live_artifact.semantic_queries = [
        {"name": "a", "measures": ["ok.count"]},
        {"name": "b", "measures": ["ok.count"], "query_context": {"timezone": "UTC"}},
    ]
    await live_artifact.asave(update_fields=["semantic_queries"])
    execute = _batch_execute({"ok.count": _ok_result})
    with patch("apps.artifacts.views.run_semantic_query", side_effect=execute) as run:
        first = await member_client.get(url)
        second = await member_client.post(url, b"", content_type="application/json")
    assert run.await_count == 2
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    key = _artifact_query_cache_key(live_artifact, "", live_artifact.semantic_queries)
    assert await cache.aget(key) == first.json()["queries"]
    assert first.content == _json_bytes(
        {
            "queries": [
                {
                    "name": "a",
                    "semantic_query": {
                        "measures": ["ok.count"],
                        "query_context": {"timezone": "UTC"},
                    },
                    "columns": ["visits.count"],
                    "rows": [[3]],
                    "row_count": 1,
                    "truncated": False,
                },
                {
                    "name": "b",
                    "semantic_query": {
                        "measures": ["ok.count"],
                        "query_context": {"timezone": "UTC"},
                    },
                    "columns": ["visits.count"],
                    "rows": [[3]],
                    "row_count": 1,
                    "truncated": False,
                },
            ],
            "query_context": None,
            "static_data": {"story_doc": {"blocks": [], "version": 1}},
            "semantic_query_manifest": {},
        }
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "full",
    [
        query_error("CONNECTION_ERROR", "full", category=CAPACITY_EXHAUSTED_CATEGORY),
        OperationalError("FATAL:  sorry, too many clients already"),
    ],
    ids=["cube_pool_full", "database_full"],
)
async def test_one_full_pool_fails_the_whole_panel_without_caching_partial_results(
    live_artifact, member_client, workspace, full
):
    await cache.aclear()
    live_artifact.semantic_queries = [
        {"name": "ok", "measures": ["ok.count"]},
        {"name": "full", "measures": ["full.count"]},
    ]
    await live_artifact.asave(update_fields=["semantic_queries"])
    execute = _batch_execute({"ok.count": _ok_result, "full.count": full})
    url = f"/api/workspaces/{workspace.id}/artifacts/{live_artifact.id}/query-data/"
    with (
        patch("apps.artifacts.views.run_semantic_query", side_effect=execute) as run,
        patch("apps.common.capacity.sentry_sdk"),
    ):
        response = await member_client.get(url)
        retried = await member_client.get(url)
    assert response.status_code == retried.status_code == 503
    assert run.await_count == 4
    key = _artifact_query_cache_key(live_artifact, "", live_artifact.semantic_queries)
    assert await cache.aget(key) is None


@pytest.mark.asyncio
async def test_runtime_check_payload_for_mixed_outcomes(monkeypatch):
    active = 0
    peak = 0
    results = {
        "ok.count": {"columns": ["ok.count"], "rows": [[1]], "row_count": 1, "truncated": True},
        "failed.count": {
            "success": False,
            "error": {"code": "QUERY_TIMEOUT", "message": "Timed out", "retryable": True},
        },
        "full.count": query_error(
            "CONNECTION_ERROR", "Cube is full", category=CAPACITY_EXHAUSTED_CATEGORY, retryable=True
        ),
        "drift.count": {"columns": ["other.count"], "rows": [[2]], "row_count": 1},
    }

    async def execute(_workspace, query, *, user_id, readiness):
        nonlocal active, peak
        assert user_id == "u1"
        active += 1
        peak = max(peak, active)
        await readiness.surface()
        await asyncio.sleep(0)
        active -= 1
        return results[query["measures"][0]]

    monkeypatch.setattr(
        "apps.artifacts.services.graph_runtime.Workspace.objects.aget",
        AsyncMock(return_value=SimpleNamespace(id="workspace")),
    )
    monkeypatch.setattr("apps.artifacts.services.graph_runtime.run_semantic_query", execute)
    inspect = AsyncMock(return_value=QuerySurfaceReadiness({"queryable": True}))
    monkeypatch.setattr("apps.semantic.services.query_outcomes.query_surface_readiness", inspect)
    queries = {
        "ok": {"measures": ["ok.count"]},
        "failed": {"measures": ["failed.count"], "limit": 5},
        "full": {"measures": ["full.count"]},
        "drift": {"measures": ["drift.count"]},
    }
    doc = {
        "schema_version": 1,
        "blocks": [
            {"id": "q", "type": "semantic_query", "hidden": True, "config": {"queries": queries}}
        ],
    }
    with freeze_time("2026-09-16T13:00:00Z"):
        result = await check_graph_artifact(
            SimpleNamespace(workspace_id="workspace", data={"story_doc": doc}), user_id="u1"
        )
    assert peak == 1
    inspect.assert_awaited_once()
    ctx = {"as_of": "2026-09-16T13:00:00+00:00", "timezone": "UTC", "today": "2026-09-16"}
    specs = [
        {"measures": ["ok.count"], "query_context": ctx, "limit": 50},
        {"measures": ["failed.count"], "limit": 5, "query_context": ctx},
        {"measures": ["full.count"], "query_context": ctx, "limit": 50},
        {"measures": ["drift.count"], "query_context": ctx, "limit": 50},
    ]
    assert inspect.await_args.args[1] == specs
    failures = [
        {
            "code": "QUERY_TIMEOUT",
            "message": "Timed out",
            "category": "runtime_failure",
            "retryable": True,
            "recovery_action": None,
        },
        {
            "code": "CONNECTION_ERROR",
            "message": "Cube is full",
            "category": "capacity_exhausted",
            "retryable": True,
            "recovery_action": None,
        },
    ]
    drift = 'Expected result key "drift_count" was not returned'
    expected = {
        "success": False,
        "query_context": ctx,
        "diagnostics": [],
        "manifest": {"schema_version": 1, "entry_count": 4, "unresolved_count": 0},
        "queries": [
            {
                "query_key": "q.ok",
                "status": "ok",
                "row_count": 1,
                "result_keys": ["ok_count"],
                "truncated": True,
                "semantic_query": specs[0],
            },
            {
                "query_key": "q.failed",
                "status": "error",
                "error": "Timed out",
                "failure": failures[0],
                "semantic_query": specs[1],
            },
            {
                "query_key": "q.full",
                "status": "error",
                "error": "Cube is full",
                "failure": failures[1],
                "semantic_query": specs[2],
            },
            {
                "query_key": "q.drift",
                "status": "ok",
                "row_count": 1,
                "result_keys": ["other_count"],
                "truncated": False,
                "semantic_query": specs[3],
            },
        ],
        "failures": [
            {"query_key": "q.failed", **failures[0]},
            {"query_key": "q.full", **failures[1]},
            {
                "query_key": "q.drift",
                "message": drift,
                "category": "invalid_document",
                "code": "RESULT_KEY_MISMATCH",
                "retryable": False,
                "recovery_action": None,
            },
        ],
        "key_warnings": [
            {
                "query_key": "q.drift",
                "message": drift,
                "expected_key": "drift_count",
                "actual_keys": ["other_count"],
            }
        ],
        "summary": "2/4 queries ok",
    }
    # The agent reads this serialized, so key order is part of the contract.
    assert json.dumps(result) == json.dumps(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised",
    [RuntimeError("cube exploded"), OperationalError("FATAL:  sorry, too many clients already")],
    ids=["bug", "database_full"],
)
async def test_runtime_check_lets_a_raised_query_error_propagate(monkeypatch, raised):
    """The agent tool reports the exception; a runtime check never swallows it as a failure."""
    monkeypatch.setattr(
        "apps.artifacts.services.graph_runtime.Workspace.objects.aget",
        AsyncMock(return_value=SimpleNamespace(id="workspace")),
    )
    monkeypatch.setattr(
        "apps.artifacts.services.graph_runtime.run_semantic_query", AsyncMock(side_effect=raised)
    )
    doc = {
        "schema_version": 1,
        "blocks": [
            {
                "id": "q",
                "type": "semantic_query",
                "hidden": True,
                "config": {"queries": {"count": {"measures": ["visits.count"]}}},
            }
        ],
    }
    with pytest.raises(type(raised)):
        await check_graph_artifact(
            SimpleNamespace(workspace_id="workspace", data={"story_doc": doc})
        )
