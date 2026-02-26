# Artifact Live Query Execution via MCP — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Re-implement `ArtifactQueryDataView` to execute `source_queries` via the MCP server's query service so "Live" artifacts display real data instead of zeros/empty charts.

**Architecture:** Convert `ArtifactQueryDataView` from a stub to an async Django view that calls `mcp_server.context.load_tenant_context` and `mcp_server.services.query.execute_query` directly (same modules the MCP `query` tool uses). This avoids an HTTP round-trip to the MCP server while reusing all existing query validation, timeout, and error-handling logic.

**Tech Stack:** Django 5 async views, psycopg3 (via mcp_server query service), pytest-django, pytest-asyncio

---

### Task 1: Write failing tests for `ArtifactQueryDataView`

**Files:**
- Create: `apps/artifacts/tests/test_artifact_query_data.py`

**Step 1: Create the test file**

```python
"""
Tests for ArtifactQueryDataView — live query execution via MCP service.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from django.test import AsyncClient
from django.urls import reverse

from apps.artifacts.models import Artifact, ArtifactType
from apps.projects.models import TenantWorkspace
from apps.users.models import TenantMembership, User


@pytest.fixture
def workspace(db):
    return TenantWorkspace.objects.create(
        tenant_id="test-domain",
        tenant_name="Test Domain",
    )


@pytest.fixture
def member_user(db, workspace):
    user = User.objects.create_user(email="member@example.com", password="pass")
    TenantMembership.objects.create(user=user, tenant_id=workspace.tenant_id)
    return user


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@example.com", password="pass")


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


FAKE_CTX = object()

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


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_returns_query_results_for_live_artifact(live_artifact, member_user):
    """Happy path: queries are executed and results returned with correct shape."""
    client = AsyncClient()
    await client.aforce_login(member_user)

    url = reverse("artifacts:query_data", kwargs={"artifact_id": live_artifact.id})

    with (
        patch(
            "apps.artifacts.views.load_tenant_context",
            new=AsyncMock(return_value=FAKE_CTX),
        ),
        patch(
            "apps.artifacts.views.execute_query",
            new=AsyncMock(side_effect=[MOCK_SUBMISSIONS_RESULT, MOCK_DAILY_RESULT]),
        ),
    ):
        response = await client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["queries"]) == 2
    assert data["queries"][0]["name"] == "submissions"
    assert data["queries"][0]["columns"] == ["total"]
    assert data["queries"][0]["rows"] == [[99]]
    assert data["queries"][1]["name"] == "daily"
    assert "error" not in data["queries"][0]
    assert data["static_data"] == {}


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_returns_empty_queries_for_static_artifact(static_artifact, member_user):
    """Artifacts with no source_queries return empty queries list."""
    client = AsyncClient()
    await client.aforce_login(member_user)

    url = reverse("artifacts:query_data", kwargs={"artifact_id": static_artifact.id})
    response = await client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["queries"] == []
    assert data["static_data"] == {"total": 42}


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_unauthenticated_returns_401(live_artifact):
    """Unauthenticated request returns 401."""
    client = AsyncClient()
    url = reverse("artifacts:query_data", kwargs={"artifact_id": live_artifact.id})
    response = await client.get(url)
    assert response.status_code == 401


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_non_member_returns_403(live_artifact, other_user):
    """User without workspace membership returns 403."""
    client = AsyncClient()
    await client.aforce_login(other_user)
    url = reverse("artifacts:query_data", kwargs={"artifact_id": live_artifact.id})
    response = await client.get(url)
    assert response.status_code == 403


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_no_workspace_returns_400(db, member_user):
    """Artifact with no workspace returns 400."""
    artifact = await Artifact.objects.acreate(
        workspace=None,
        created_by=member_user,
        title="Orphan",
        artifact_type=ArtifactType.REACT,
        code="x",
        conversation_id="t",
        source_queries=[{"name": "q", "sql": "SELECT 1"}],
    )
    client = AsyncClient()
    await client.aforce_login(member_user)
    url = reverse("artifacts:query_data", kwargs={"artifact_id": artifact.id})
    response = await client.get(url)
    assert response.status_code == 400


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_tenant_context_error_returns_error_query(live_artifact, member_user):
    """If load_tenant_context fails (no schema), return error response."""
    client = AsyncClient()
    await client.aforce_login(member_user)
    url = reverse("artifacts:query_data", kwargs={"artifact_id": live_artifact.id})

    with patch(
        "apps.artifacts.views.load_tenant_context",
        new=AsyncMock(side_effect=ValueError("No active schema")),
    ):
        response = await client.get(url)

    assert response.status_code == 200
    data = response.json()
    # All queries should have errors
    assert all("error" in q for q in data["queries"])


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_individual_query_failure_continues(live_artifact, member_user):
    """A failed query includes an error entry; other queries still execute."""
    client = AsyncClient()
    await client.aforce_login(member_user)
    url = reverse("artifacts:query_data", kwargs={"artifact_id": live_artifact.id})

    error_result = {"success": False, "error": {"code": "QUERY_TIMEOUT", "message": "Timed out"}}

    with (
        patch(
            "apps.artifacts.views.load_tenant_context",
            new=AsyncMock(return_value=FAKE_CTX),
        ),
        patch(
            "apps.artifacts.views.execute_query",
            new=AsyncMock(side_effect=[error_result, MOCK_DAILY_RESULT]),
        ),
    ):
        response = await client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["queries"]) == 2
    assert "error" in data["queries"][0]
    assert data["queries"][0]["name"] == "submissions"
    assert data["queries"][1]["name"] == "daily"
    assert "error" not in data["queries"][1]
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/bderenzi/Code/scout-bdr-issue-19-mcp-artifact-queries
uv run pytest apps/artifacts/tests/test_artifact_query_data.py -v 2>&1 | head -60
```

Expected: multiple FAILs — the view currently returns `queries: []` always, so happy path fails; 401/403/400 checks likely fail too since the view has no membership check.

**Step 3: Commit the failing tests**

```bash
git add apps/artifacts/tests/test_artifact_query_data.py
git commit -m "test: add failing tests for ArtifactQueryDataView MCP execution"
```

---

### Task 2: Implement `ArtifactQueryDataView` with MCP query execution

**Files:**
- Modify: `apps/artifacts/views.py:733-752`

**Step 1: Add imports at top of file (near existing imports)**

Find the existing imports block at the top of `apps/artifacts/views.py` and add these two lines near the other imports (they can go right after the existing `from django...` imports):

```python
from mcp_server.context import load_tenant_context
from mcp_server.services.query import execute_query
```

**Step 2: Replace the `ArtifactQueryDataView` class**

Replace lines 733-752 (the entire `ArtifactQueryDataView` class) with:

```python
class ArtifactQueryDataView(View):
    """
    Executes an artifact's source_queries via the MCP query service and returns results.

    For artifacts with source_queries, each SQL query is executed against the tenant's
    database using the same query service as the MCP server. Results are returned in a
    format the artifact sandbox can consume directly via mergeQueryResults().
    """

    async def get(self, request: HttpRequest, artifact_id: str) -> JsonResponse:
        artifact = await Artifact.objects.select_related("workspace").aget_or_404(
            pk=artifact_id
        )

        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)

        if not request.user.is_superuser and artifact.workspace_id:
            from apps.users.models import TenantMembership

            has_access = await TenantMembership.objects.filter(
                user=request.user, tenant_id=artifact.workspace.tenant_id
            ).aexists()
            if not has_access:
                return JsonResponse({"error": "Access denied"}, status=403)

        if not artifact.source_queries:
            return JsonResponse({"queries": [], "static_data": artifact.data or {}})

        if artifact.workspace is None:
            return JsonResponse({"error": "Artifact has no associated workspace"}, status=400)

        # Load tenant context once for all queries
        try:
            ctx = await load_tenant_context(artifact.workspace.tenant_id)
        except (ValueError, Exception) as e:
            error_msg = str(e)
            results = [
                {"name": entry.get("name", f"query_{i}"), "error": error_msg}
                for i, entry in enumerate(artifact.source_queries)
            ]
            return JsonResponse({"queries": results, "static_data": artifact.data or {}})

        results = []
        for i, entry in enumerate(artifact.source_queries):
            name = entry.get("name", f"query_{i}")
            sql = entry.get("sql", "")
            if not sql:
                results.append({"name": name, "error": "Empty SQL query"})
                continue

            result = await execute_query(ctx, sql)

            if not result.get("success", True) or result.get("error"):
                error_info = result.get("error", {})
                msg = error_info.get("message", "Query failed") if isinstance(error_info, dict) else str(error_info)
                results.append({"name": name, "error": msg})
            else:
                results.append(
                    {
                        "name": name,
                        "columns": result.get("columns", []),
                        "rows": result.get("rows", []),
                        "row_count": result.get("row_count", 0),
                        "truncated": result.get("truncated", False),
                    }
                )

        return JsonResponse({"queries": results, "static_data": artifact.data or {}})
```

**Note on `aget_or_404`:** Django's async ORM doesn't have `aget_or_404`. Use this pattern instead:

```python
from django.http import Http404

try:
    artifact = await Artifact.objects.select_related("workspace").aget(pk=artifact_id)
except Artifact.DoesNotExist:
    raise Http404
```

So the actual full replacement is:

```python
class ArtifactQueryDataView(View):
    """
    Executes an artifact's source_queries via the MCP query service and returns results.

    For artifacts with source_queries, each SQL query is executed against the tenant's
    database using the same query service as the MCP server. Results are returned in a
    format the artifact sandbox can consume directly via mergeQueryResults().
    """

    async def get(self, request: HttpRequest, artifact_id: str) -> JsonResponse:
        from django.http import Http404

        try:
            artifact = await Artifact.objects.select_related("workspace").aget(pk=artifact_id)
        except Artifact.DoesNotExist:
            raise Http404

        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)

        if not request.user.is_superuser and artifact.workspace_id:
            from apps.users.models import TenantMembership

            has_access = await TenantMembership.objects.filter(
                user=request.user, tenant_id=artifact.workspace.tenant_id
            ).aexists()
            if not has_access:
                return JsonResponse({"error": "Access denied"}, status=403)

        if not artifact.source_queries:
            return JsonResponse({"queries": [], "static_data": artifact.data or {}})

        if artifact.workspace is None:
            return JsonResponse({"error": "Artifact has no associated workspace"}, status=400)

        try:
            ctx = await load_tenant_context(artifact.workspace.tenant_id)
        except Exception as e:
            error_msg = str(e)
            results = [
                {"name": entry.get("name", f"query_{i}"), "error": error_msg}
                for i, entry in enumerate(artifact.source_queries)
            ]
            return JsonResponse({"queries": results, "static_data": artifact.data or {}})

        results = []
        for i, entry in enumerate(artifact.source_queries):
            name = entry.get("name", f"query_{i}")
            sql = entry.get("sql", "")
            if not sql:
                results.append({"name": name, "error": "Empty SQL query"})
                continue

            result = await execute_query(ctx, sql)

            if not result.get("success", True) or result.get("error"):
                error_info = result.get("error", {})
                msg = (
                    error_info.get("message", "Query failed")
                    if isinstance(error_info, dict)
                    else str(error_info)
                )
                results.append({"name": name, "error": msg})
            else:
                results.append(
                    {
                        "name": name,
                        "columns": result.get("columns", []),
                        "rows": result.get("rows", []),
                        "row_count": result.get("row_count", 0),
                        "truncated": result.get("truncated", False),
                    }
                )

        return JsonResponse({"queries": results, "static_data": artifact.data or {}})
```

**Step 3: Run the new tests**

```bash
cd /Users/bderenzi/Code/scout-bdr-issue-19-mcp-artifact-queries
uv run pytest apps/artifacts/tests/test_artifact_query_data.py -v 2>&1
```

Expected: All 7 tests PASS.

**Step 4: Run the full test suite to check for regressions**

```bash
uv run pytest apps/artifacts/ -v 2>&1
```

Expected: All existing tests still pass.

**Step 5: Run linter**

```bash
uv run ruff check apps/artifacts/views.py
uv run ruff format apps/artifacts/views.py
```

Expected: No errors.

**Step 6: Commit**

```bash
git add apps/artifacts/views.py
git commit -m "feat: execute artifact source_queries via MCP service (fixes #19)"
```

---

### Task 3: Verify end-to-end with the running app

**Step 1: Create log directory**

```bash
mkdir -p /tmp/bdr-issue-19-mcp-artifact-queries
```

**Step 2: Start dependencies**

```bash
docker compose up platform-db redis mcp-server -d
```

Wait ~10 seconds for containers to be healthy.

**Step 3: Start Django**

```bash
cd /Users/bderenzi/Code/scout-bdr-issue-19-mcp-artifact-queries
uv run uvicorn config.asgi:application --reload --port 8001 \
  >> /tmp/bdr-issue-19-mcp-artifact-queries/django.log 2>&1 &
```

**Step 4: Start frontend**

```bash
cd /Users/bderenzi/Code/scout-bdr-issue-19-mcp-artifact-queries/frontend
bun dev --port 5174 >> /tmp/bdr-issue-19-mcp-artifact-queries/frontend.log 2>&1 &
```

**Step 5: Use playwright-cli to verify a live artifact renders data**

```bash
mkdir -p /tmp/bdr-issue-19-mcp-artifact-queries/screenshots
playwright-cli screenshot http://localhost:5174 \
  /tmp/bdr-issue-19-mcp-artifact-queries/screenshots/artifacts-gallery.png
```

Navigate to an artifact tagged "Live" and verify chart/data is populated (not zeros).

**Step 6: Check the query-data endpoint directly**

Find a live artifact ID from the DB or UI, then:

```bash
# Replace <ARTIFACT_ID> with a real UUID
curl -s http://localhost:8001/api/artifacts/<ARTIFACT_ID>/query-data/ \
  -H "Cookie: <session_cookie>" | python3 -m json.tool
```

Expected: `queries` array with real `columns`/`rows` data, not `[]`.

---

## Summary of Files Changed

| File | Change |
|------|--------|
| `apps/artifacts/views.py` | Replace stub `ArtifactQueryDataView` with async implementation |
| `apps/artifacts/tests/test_artifact_query_data.py` | New test file (7 tests) |

No migrations, no model changes, no frontend changes.
