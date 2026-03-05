# Explicit Workspace Context Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the implicit `last_selected_at`-based workspace resolution used by knowledge, recipes, and artifacts APIs with explicit URL path-scoped routes, making the tenant context visible, cache-friendly, and self-contained in every request.

**The problem:** `apps/knowledge/api/views.py`, `apps/recipes/api/views.py`, `apps/artifacts/views.py`, and `apps/projects/api/views.py` all resolve the active workspace by querying `TenantMembership.objects.filter(user=request.user).order_by(F("last_selected_at").desc())`. This is server-side global state: the last tenant the user's browser posted to `/api/auth/tenants/select/` wins for all tabs and sessions. This makes API responses non-deterministic for the same URL, breaks multi-tab usage, and is invisible in logs.

**The fix:** Scope all tenant-specific resource URLs under `/<resource>/<tenant_id>/` where `tenant_id` is the `TenantMembership.id` UUID. The workspace context is then part of the resource identity — visible in the URL, validated on every request, and independent of any server-side selection state.

**URL structure (before → after):**

```
# Knowledge
GET  /api/knowledge/                        →  GET  /api/knowledge/<tenant_id>/
POST /api/knowledge/                        →  POST /api/knowledge/<tenant_id>/
GET  /api/knowledge/export/                 →  GET  /api/knowledge/<tenant_id>/export/
POST /api/knowledge/import/                 →  POST /api/knowledge/<tenant_id>/import/
GET  /api/knowledge/<item_id>/              →  GET  /api/knowledge/<tenant_id>/<item_id>/
PUT  /api/knowledge/<item_id>/              →  PUT  /api/knowledge/<tenant_id>/<item_id>/
DELETE /api/knowledge/<item_id>/            →  DELETE /api/knowledge/<tenant_id>/<item_id>/

# Recipes
GET  /api/recipes/                          →  GET  /api/recipes/<tenant_id>/
GET  /api/recipes/<recipe_id>/              →  GET  /api/recipes/<tenant_id>/<recipe_id>/
PUT  /api/recipes/<recipe_id>/              →  PUT  /api/recipes/<tenant_id>/<recipe_id>/
DELETE /api/recipes/<recipe_id>/            →  DELETE /api/recipes/<tenant_id>/<recipe_id>/
POST /api/recipes/<recipe_id>/run/          →  POST /api/recipes/<tenant_id>/<recipe_id>/run/
GET  /api/recipes/<recipe_id>/runs/         →  GET  /api/recipes/<tenant_id>/<recipe_id>/runs/
GET  /api/recipes/<recipe_id>/runs/<run_id>/→  GET  /api/recipes/<tenant_id>/<recipe_id>/runs/<run_id>/

# Artifacts
GET  /api/artifacts/                        →  GET  /api/artifacts/<tenant_id>/
PATCH /api/artifacts/<artifact_id>/         →  PATCH /api/artifacts/<tenant_id>/<artifact_id>/
DELETE /api/artifacts/<artifact_id>/        →  DELETE /api/artifacts/<tenant_id>/<artifact_id>/
GET  /api/artifacts/<artifact_id>/sandbox/  →  GET  /api/artifacts/<tenant_id>/<artifact_id>/sandbox/
GET  /api/artifacts/<artifact_id>/data/     →  GET  /api/artifacts/<tenant_id>/<artifact_id>/data/
GET  /api/artifacts/<artifact_id>/query-data/ → GET /api/artifacts/<tenant_id>/<artifact_id>/query-data/
POST /api/artifacts/<artifact_id>/share/    →  POST /api/artifacts/<tenant_id>/<artifact_id>/share/
GET  /api/artifacts/<artifact_id>/shares/   →  GET  /api/artifacts/<tenant_id>/<artifact_id>/shares/
DELETE /api/artifacts/<artifact_id>/shares/<token>/ → DELETE /api/artifacts/<tenant_id>/<artifact_id>/shares/<token>/
GET  /api/artifacts/<artifact_id>/export/<fmt>/ → GET /api/artifacts/<tenant_id>/<artifact_id>/export/<fmt>/

# Public/shared routes — no tenant_id required, unchanged:
GET  /api/artifacts/shared/<token>/         →  unchanged
GET  /api/recipes/runs/shared/<token>/      →  unchanged
```

`tenant_id` is always the `TenantMembership.id` UUID (same value as `activeDomainId` in the frontend store). It must belong to the requesting user — validated on every request.

**Reference (correct pattern):** `apps/chat/views.py:559` — the chat view already validates tenant ownership explicitly:
```python
tenant_membership = await TenantMembership.objects.aget(id=tenant_id, user=user)
```

**Tech Stack:** Django 5 + DRF, React 19 + Zustand, TypeScript.

---

## Phase 1: Tests

### Task 1: Write failing tests that document the current flaw

**Files:**
- Create: `tests/test_explicit_workspace_context.py`

**Step 1: Write the tests**

These tests verify the desired behavior against the NEW URL structure — they should FAIL against the current code.

```python
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.projects.models import TenantWorkspace
from apps.users.models import TenantMembership

User = get_user_model()


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def user_a(db):
    return User.objects.create_user(email="user_a@test.com", password="pass")


@pytest.fixture
def user_b(db):
    return User.objects.create_user(email="user_b@test.com", password="pass")


@pytest.fixture
def membership_a(user_a):
    return TenantMembership.objects.create(
        user=user_a, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )


@pytest.fixture
def membership_b(user_a):
    return TenantMembership.objects.create(
        user=user_a, provider="commcare", tenant_id="domain-b", tenant_name="Domain B"
    )


@pytest.fixture
def workspace_a(membership_a):
    ws, _ = TenantWorkspace.objects.get_or_create(
        tenant_id="domain-a", defaults={"tenant_name": "Domain A"}
    )
    return ws


@pytest.fixture
def workspace_b(membership_b):
    ws, _ = TenantWorkspace.objects.get_or_create(
        tenant_id="domain-b", defaults={"tenant_name": "Domain B"}
    )
    return ws


class TestKnowledgeExplicitTenantId:
    def test_scoped_url_returns_200(self, client, user_a, membership_a, workspace_a):
        """GET /api/knowledge/<tenant_id>/ returns 200 for a valid tenant."""
        client.force_login(user_a)
        response = client.get(f"/api/knowledge/{membership_a.id}/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a, membership_a, workspace_a):
        """GET /api/knowledge/ (old URL) is no longer valid."""
        client.force_login(user_a)
        response = client.get("/api/knowledge/")
        assert response.status_code == 404

    def test_correct_workspace_is_used(
        self, client, user_a, membership_a, membership_b, workspace_a, workspace_b
    ):
        """The tenant_id in the URL determines which workspace's data is returned."""
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(
            workspace=workspace_a, title="Entry A", content="content"
        )
        KnowledgeEntry.objects.create(
            workspace=workspace_b, title="Entry B", content="content"
        )

        # Mark membership_b as most recently selected — must have no effect
        from django.utils import timezone
        membership_b.last_selected_at = timezone.now()
        membership_b.save(update_fields=["last_selected_at"])

        client.force_login(user_a)

        response = client.get(f"/api/knowledge/{membership_a.id}/")
        assert response.status_code == 200
        titles = [item["title"] for item in response.json().get("items", response.json())]
        assert "Entry A" in titles
        assert "Entry B" not in titles

    def test_foreign_tenant_id_returns_403(self, client, user_a, user_b, db):
        """A tenant_id belonging to another user returns 403."""
        other_membership = TenantMembership.objects.create(
            user=user_b, provider="commcare", tenant_id="domain-other", tenant_name="Other"
        )
        client.force_login(user_a)
        response = client.get(f"/api/knowledge/{other_membership.id}/")
        assert response.status_code == 403


class TestRecipesExplicitTenantId:
    def test_scoped_url_returns_200(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get(f"/api/recipes/{membership_a.id}/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get("/api/recipes/")
        assert response.status_code == 404

    def test_correct_workspace_is_used(
        self, client, user_a, membership_a, membership_b, workspace_a, workspace_b
    ):
        from apps.recipes.models import Recipe

        Recipe.objects.create(workspace=workspace_a, title="Recipe A", prompt_template="")
        Recipe.objects.create(workspace=workspace_b, title="Recipe B", prompt_template="")

        from django.utils import timezone
        membership_b.last_selected_at = timezone.now()
        membership_b.save(update_fields=["last_selected_at"])

        client.force_login(user_a)
        response = client.get(f"/api/recipes/{membership_a.id}/")
        assert response.status_code == 200
        titles = [r["title"] for r in response.json()]
        assert "Recipe A" in titles
        assert "Recipe B" not in titles

    def test_foreign_tenant_id_returns_403(self, client, user_a, user_b, db):
        other_membership = TenantMembership.objects.create(
            user=user_b, provider="commcare", tenant_id="domain-other2", tenant_name="Other2"
        )
        client.force_login(user_a)
        response = client.get(f"/api/recipes/{other_membership.id}/")
        assert response.status_code == 403


class TestArtifactsExplicitTenantId:
    def test_scoped_url_returns_200(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get(f"/api/artifacts/{membership_a.id}/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get("/api/artifacts/")
        assert response.status_code == 404

    def test_foreign_tenant_id_returns_403(self, client, user_a, user_b, db):
        other_membership = TenantMembership.objects.create(
            user=user_b, provider="commcare", tenant_id="domain-other3", tenant_name="Other3"
        )
        client.force_login(user_a)
        response = client.get(f"/api/artifacts/{other_membership.id}/")
        assert response.status_code == 403
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/test_explicit_workspace_context.py -x -v
```

Expected: FAIL — the new URLs don't exist yet.

**Step 3: Commit**

```bash
git add -A && git commit -m "test: add failing tests for path-scoped tenant workspace resolution"
```

---

## Phase 2: Backend — Restructure URLs and views

### Task 2: Add `tenant_id` path parameter to knowledge views and URLs

**Files:**
- Modify: `apps/knowledge/api/views.py`
- Modify: `apps/knowledge/urls.py`

**Step 1: Replace `_resolve_workspace` in `apps/knowledge/api/views.py`**

Replace the existing `_resolve_workspace` function (lines 39–60) with a version that reads `tenant_id` from the URL kwargs, passed in by the view:

```python
def _resolve_workspace(request, tenant_id):
    """Resolve TenantWorkspace from the tenant_id URL path parameter.

    tenant_id is the TenantMembership.id (UUID). It must belong to the
    requesting user. Returns (workspace, None) on success or (None, Response)
    on error.
    """
    from apps.projects.models import TenantWorkspace
    from apps.users.models import TenantMembership

    try:
        membership = TenantMembership.objects.get(id=tenant_id, user=request.user)
    except TenantMembership.DoesNotExist:
        return None, Response(
            {"error": "Tenant not found or access denied."},
            status=status.HTTP_403_FORBIDDEN,
        )

    workspace, _ = TenantWorkspace.objects.get_or_create(
        tenant_id=membership.tenant_id,
        defaults={"tenant_name": membership.tenant_name},
    )
    return workspace, None
```

Remove the `_resolve_custom_workspace` function entirely — custom workspace support is not in scope for this change.

Remove the `from django.db.models import F` import if it was only used by the old `_resolve_workspace`.

**Step 2: Update all view methods in `apps/knowledge/api/views.py` to accept `tenant_id`**

Each view method currently calls `_resolve_workspace(request)`. Update every method signature to accept `tenant_id` from the URL and pass it through:

```python
# Before:
class KnowledgeListCreateView(APIView):
    def get(self, request):
        workspace, err = _resolve_workspace(request)

# After:
class KnowledgeListCreateView(APIView):
    def get(self, request, tenant_id):
        workspace, err = _resolve_workspace(request, tenant_id)

    def post(self, request, tenant_id):
        workspace, err = _resolve_workspace(request, tenant_id)
```

Apply this to every view class and method that calls `_resolve_workspace`: `KnowledgeListCreateView`, `KnowledgeDetailView`, `KnowledgeExportView`, `KnowledgeImportView`. For `KnowledgeDetailView`, `item_id` also comes from the URL — keep it alongside `tenant_id`:

```python
class KnowledgeDetailView(APIView):
    def get(self, request, tenant_id, item_id): ...
    def put(self, request, tenant_id, item_id): ...
    def delete(self, request, tenant_id, item_id): ...
```

**Step 3: Update `apps/knowledge/urls.py`**

Replace the existing urlpatterns with tenant-scoped paths:

```python
urlpatterns = [
    path("<uuid:tenant_id>/", KnowledgeListCreateView.as_view(), name="list_create"),
    path("<uuid:tenant_id>/export/", KnowledgeExportView.as_view(), name="export"),
    path("<uuid:tenant_id>/import/", KnowledgeImportView.as_view(), name="import"),
    path("<uuid:tenant_id>/<uuid:item_id>/", KnowledgeDetailView.as_view(), name="detail"),
]
```

---

### Task 3: Add `tenant_id` path parameter to recipes views and URLs

**Files:**
- Modify: `apps/recipes/api/views.py`
- Modify: `apps/recipes/urls.py`

**Step 1: Replace `_resolve_workspace` in `apps/recipes/api/views.py`**

Same replacement as Task 2 — `_resolve_workspace(request, tenant_id)` reading from the path parameter.

**Step 2: Update all view method signatures to accept `tenant_id`**

```python
class RecipeListView(APIView):
    def get(self, request, tenant_id): ...

class RecipeDetailView(APIView):
    def _get_recipe(self, request, tenant_id, recipe_id): ...
    def get(self, request, tenant_id, recipe_id): ...
    def put(self, request, tenant_id, recipe_id): ...
    def delete(self, request, tenant_id, recipe_id): ...

class RecipeRunView(APIView):
    def post(self, request, tenant_id, recipe_id): ...

class RecipeRunListView(APIView):
    def get(self, request, tenant_id, recipe_id): ...

class RecipeRunDetailView(APIView):
    def get(self, request, tenant_id, recipe_id, run_id): ...
    def patch(self, request, tenant_id, recipe_id, run_id): ...
```

**Step 3: Update `apps/recipes/urls.py`**

```python
urlpatterns = [
    path("<uuid:tenant_id>/", RecipeListView.as_view(), name="list"),
    path("<uuid:tenant_id>/<uuid:recipe_id>/", RecipeDetailView.as_view(), name="detail"),
    path("<uuid:tenant_id>/<uuid:recipe_id>/run/", RecipeRunView.as_view(), name="run"),
    path("<uuid:tenant_id>/<uuid:recipe_id>/runs/", RecipeRunListView.as_view(), name="runs"),
    path(
        "<uuid:tenant_id>/<uuid:recipe_id>/runs/<uuid:run_id>/",
        RecipeRunDetailView.as_view(),
        name="run_detail",
    ),
]
```

---

### Task 4: Add `tenant_id` path parameter to artifacts views and URLs

**Files:**
- Modify: `apps/artifacts/views.py`
- Modify: `apps/artifacts/urls.py`

**Step 1: Update `ArtifactListView` in `apps/artifacts/views.py`**

Replace the inline `last_selected_at` resolution block (around line 950) with:

```python
def get(self, request: HttpRequest, tenant_id) -> JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)

    from apps.projects.models import TenantWorkspace
    from apps.users.models import TenantMembership

    try:
        membership = TenantMembership.objects.get(id=tenant_id, user=request.user)
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found or access denied."}, status=403)

    workspace, _ = TenantWorkspace.objects.get_or_create(
        tenant_id=membership.tenant_id,
        defaults={"tenant_name": membership.tenant_name},
    )
    # ... rest of the method unchanged
```

**Step 2: Update all other artifact view methods to accept `tenant_id`**

`ArtifactDetailView`, `ArtifactSandboxView`, `ArtifactDataView`, `ArtifactQueryDataView`, `ArtifactExportView`, and the share API views (`CreateShareView`, `ListSharesView`, `RevokeShareView`) must all accept `tenant_id` and validate it via the same pattern. These views currently look up the artifact by `artifact_id` and verify ownership via `workspace` — they must now also validate the `tenant_id` path parameter.

For any view that fetches an artifact by ID, the validation pattern is:

```python
def get(self, request, tenant_id, artifact_id):
    # Validate tenant ownership
    try:
        membership = TenantMembership.objects.get(id=tenant_id, user=request.user)
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found or access denied."}, status=403)

    workspace, _ = TenantWorkspace.objects.get_or_create(
        tenant_id=membership.tenant_id,
        defaults={"tenant_name": membership.tenant_name},
    )
    # Then fetch artifact scoped to that workspace
    artifact = get_object_or_404(Artifact, id=artifact_id, workspace=workspace)
```

**Step 3: Update `apps/artifacts/urls.py`**

The `shared/<token>/` route has no tenant context and stays unchanged. All other routes get the `<tenant_id>/` prefix:

```python
urlpatterns = [
    # Public — no tenant required
    path("shared/<str:share_token>/", SharedArtifactView.as_view(), name="shared"),

    # Tenant-scoped
    path("<uuid:tenant_id>/", ArtifactListView.as_view(), name="list"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/", ArtifactDetailView.as_view(), name="detail"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/sandbox/", ArtifactSandboxView.as_view(), name="sandbox"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/data/", ArtifactDataView.as_view(), name="data"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/query-data/", ArtifactQueryDataView.as_view(), name="query_data"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/share/", CreateShareView.as_view(), name="create_share"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/shares/", ListSharesView.as_view(), name="list_shares"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/shares/<str:share_token>/", RevokeShareView.as_view(), name="revoke_share"),
    path("<uuid:tenant_id>/<uuid:artifact_id>/export/<str:format>/", ArtifactExportView.as_view(), name="export"),
]
```

---

### Task 5: Update `apps/projects/api/views.py`

**Files:**
- Modify: `apps/projects/api/views.py`

Apply the same `_resolve_workspace(request, tenant_id)` replacement. Check the URL configuration for `apps/projects` to understand how `tenant_id` reaches these views and update accordingly.

---

### Task 6: Run backend tests

```bash
uv run pytest tests/test_explicit_workspace_context.py -x -v
```

Expected: All pass.

```bash
uv run pytest tests/ -x -q
```

Fix any failures from existing tests that use the old URL structure — update those test URLs to include the `tenant_id` path segment.

```bash
uv run ruff check . && uv run ruff format .
```

**Commit:**

```bash
git add -A && git commit -m "fix: scope knowledge, recipe, and artifact URLs under /<resource>/<tenant_id>/"
```

---

## Phase 3: Frontend — Update API call URLs

### Task 7: Update knowledge, recipes, and artifacts slices

**Files:**
- Modify: `frontend/src/store/knowledgeSlice.ts`
- Modify: `frontend/src/store/recipeSlice.ts`
- Modify: `frontend/src/store/artifactSlice.ts`

Each slice currently omits the tenant from the URL. The `activeDomainId` in the Zustand store (`DomainSlice.activeDomainId`) is the `TenantMembership.id` — exactly the value the new URL paths require.

**Step 1: Update `knowledgeSlice.ts`**

Read `activeDomainId` via `get().activeDomainId` in each action and insert it as the first path segment:

```typescript
// Before:
const url = `/api/knowledge/${queryString ? `?${queryString}` : ""}`

// After:
const activeDomainId = get().activeDomainId
if (!activeDomainId) throw new Error("No active domain selected.")
const url = `/api/knowledge/${activeDomainId}/${queryString ? `?${queryString}` : ""}`
```

Apply to all call sites: `fetchKnowledge`, `createKnowledgeItem`, `updateKnowledgeItem` (`/api/knowledge/${activeDomainId}/${id}/`), `deleteKnowledgeItem`, `exportKnowledge`, `importKnowledge`.

**Step 2: Update `recipeSlice.ts`**

```typescript
// Before:
const recipes = await api.get<Recipe[]>(`/api/recipes/`)
const recipe = await api.get<Recipe>(`/api/recipes/${recipeId}/`)

// After:
const activeDomainId = get().activeDomainId
if (!activeDomainId) throw new Error("No active domain selected.")
const recipes = await api.get<Recipe[]>(`/api/recipes/${activeDomainId}/`)
const recipe = await api.get<Recipe>(`/api/recipes/${activeDomainId}/${recipeId}/`)
```

Apply to all call sites: `fetchRecipes`, `fetchRecipe`, `updateRecipe`, `deleteRecipe`, `runRecipe`, `fetchRecipeRuns`, `fetchRecipeRun`, `updateRecipeRun`.

**Step 3: Update `artifactSlice.ts`**

```typescript
// Before:
const url = `/api/artifacts/${qs ? `?${qs}` : ""}`

// After:
const activeDomainId = get().activeDomainId
if (!activeDomainId) throw new Error("No active domain selected.")
const url = `/api/artifacts/${activeDomainId}/${qs ? `?${qs}` : ""}`
```

Apply to all call sites: `fetchArtifacts`, `fetchArtifact` (`/api/artifacts/${activeDomainId}/${artifactId}/`), `deleteArtifact`.

**Step 4: Update any component-level artifact URLs**

`frontend/src/components/ArtifactPanel/ArtifactPanel.tsx` constructs artifact URLs directly. Search for hardcoded `/api/artifacts/` URLs in components and update them to include `activeDomainId`.

**Step 5: Run frontend lint**

```bash
cd frontend && bun run lint
```

Fix any type errors.

**Step 6: Commit**

```bash
git add -A && git commit -m "feat: update frontend slices to use tenant-scoped resource URLs"
```

---

## Phase 4: Clean up `last_selected_at`

### Task 8: Verify and document `last_selected_at` scope

**Step 1: Verify no remaining backend uses**

```bash
grep -rn "last_selected_at" apps/ --include="*.py"
```

After Phases 2 and 3, the only remaining uses should be:
- `apps/users/views.py` — `tenant_select_view` still sets `last_selected_at`; `tenant_list_view` returns it in the response
- `apps/users/models.py` — field declaration and model `ordering`
- Migrations — historical, leave untouched

**Step 2: Note on the field**

Do NOT remove `last_selected_at` in this change. It still serves a valid UX purpose: the frontend uses it to order tenants so the most recently used appears first in the selector. Removing the DB column requires a migration and is a separate decision.

**Step 3: Add clarifying comment**

In `apps/users/views.py`, above `tenant_select_view`, add:

```python
# last_selected_at is a UX ordering hint only.
# It does NOT affect API workspace resolution — all resource endpoints
# use explicit tenant_id path parameters.
```

**Step 4: Commit**

```bash
git add -A && git commit -m "docs: clarify last_selected_at is UX ordering only, not workspace resolution"
```

---

## Summary

| # | Task | Depends On |
|---|------|------------|
| 1 | Write failing tests | — |
| 2 | knowledge views + URLs | 1 |
| 3 | recipes views + URLs | 1 |
| 4 | artifacts views + URLs | 1 |
| 5 | projects views + URLs | 1 |
| 6 | Run full backend test suite | 2–5 |
| 7 | Update frontend slices | 6 |
| 8 | Verify and document `last_selected_at` | 7 |

After this plan, every API request is self-contained: the workspace context is part of the URL, validated on every request, independent of any server-side selection state. Multi-tab usage works correctly. Requests are reproducible and auditable in logs.
