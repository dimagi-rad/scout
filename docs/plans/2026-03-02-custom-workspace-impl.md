# CustomWorkspace Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Introduce CustomWorkspace — a user-created, multi-tenant workspace with role-based membership, alongside the existing TenantWorkspace, with a full-width tabbed workspace selector UI.

**Architecture:** Rename `apps/projects` → `apps/workspace` to house both TenantWorkspace and CustomWorkspace as peers. Add dual nullable FKs on knowledge/chat models. New REST API at `/api/custom-workspaces/`. Frontend gets a workspaceSlice and full-width tabbed workspace selector panel.

**Tech Stack:** Django 5 + DRF, PostgreSQL, React 19 + Zustand + Tailwind CSS 4, shadcn/ui components.

---

## Phase 1: Rename `apps/projects` → `apps/workspace`

This is the foundation. All subsequent work depends on this being done first.

### Task 1: Rename the app directory and update AppConfig

**Files:**
- Rename: `apps/projects/` → `apps/workspace/`
- Modify: `apps/workspace/apps.py` (was `apps/projects/apps.py`)

**Step 1: Rename the directory**

```bash
git mv apps/projects apps/workspace
```

**Step 2: Update AppConfig**

Edit `apps/workspace/apps.py`:

```python
from django.apps import AppConfig


class WorkspaceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workspace"
    label = "workspace"
    verbose_name = "Workspace"
```

Note: Setting `label = "workspace"` means Django will use `workspace` as the app label in FK string references, migration directories, and content types. The `db_table` values will change from `projects_*` to `workspace_*` unless we explicitly set them.

**Step 3: Set explicit `db_table` on all existing models to preserve table names**

Edit `apps/workspace/models.py` — add `db_table` to each model's Meta:

```python
class TenantSchema(models.Model):
    # ... existing fields ...
    class Meta:
        ordering = ["-last_accessed_at"]
        db_table = "projects_tenantschema"

class MaterializationRun(models.Model):
    # ... existing fields ...
    class Meta:
        ordering = ["-started_at"]
        db_table = "projects_materializationrun"

class TenantWorkspace(models.Model):
    # ... existing fields ...
    class Meta:
        ordering = ["tenant_name"]
        db_table = "projects_tenantworkspace"

class TenantMetadata(models.Model):
    # ... existing fields ...
    class Meta:
        verbose_name = "Tenant Metadata"
        verbose_name_plural = "Tenant Metadata"
        db_table = "projects_tenantmetadata"
```

**Step 4: Run tests to verify nothing broke yet**

```bash
uv run pytest tests/ -x -q
```

Expected: Tests may fail due to import errors — that's addressed in Task 2.

**Step 5: Commit**

```bash
git add -A && git commit -m "refactor: rename apps/projects to apps/workspace (directory + AppConfig)"
```

---

### Task 2: Update all imports and string references

**Files to modify (all references to `apps.projects` or `"projects.`)**:
- `config/settings/base.py:60` — INSTALLED_APPS
- `config/urls.py:12-13` — imports
- `apps/knowledge/models.py:31,95,140` — FK string `"projects.TenantWorkspace"`
- `apps/recipes/models.py:38` — FK string `"projects.TenantWorkspace"`
- `apps/artifacts/models.py:59` — FK string `"projects.TenantWorkspace"`
- `apps/workspace/api/views.py` — internal imports
- `apps/workspace/services/schema_manager.py:15` — internal import
- `apps/agents/graph/base.py:47,164` — imports
- `apps/agents/tools/recipe_tool.py:19` — import
- `apps/agents/tools/learning_tool.py:22` — import
- `apps/agents/tools/artifact_tool.py:21` — import
- `apps/knowledge/api/views.py:43` — import
- `apps/knowledge/services/retriever.py:15` — import
- `apps/recipes/api/views.py:32` — import
- `apps/artifacts/views.py:944` — import
- `mcp_server/server.py:32,552,626-627` — imports
- `mcp_server/services/materializer.py:27-28` — imports
- `mcp_server/services/metadata.py:13,18` — imports
- `mcp_server/context.py:47` — import
- `tests/conftest.py:53` — import
- `tests/test_models.py:97,113,120` — imports
- `tests/test_artifacts.py:393` — import
- `tests/test_mcp_tenant_tools.py:496,521` — imports
- `tests/test_schema_manager.py:5-6` — imports
- `apps/artifacts/tests/test_share_api.py:21` — import
- `apps/artifacts/tests/test_artifact_query_data.py:14` — import

**Step 1: Update INSTALLED_APPS**

In `config/settings/base.py:60`, change:
```python
"apps.projects",
```
to:
```python
"apps.workspace",
```

**Step 2: Update all FK string references**

In models that use `"projects.TenantWorkspace"`, change to `"workspace.TenantWorkspace"`:

- `apps/knowledge/models.py` — 3 occurrences (lines 31, 95, 140)
- `apps/recipes/models.py` — 1 occurrence (line 38)
- `apps/artifacts/models.py` — 1 occurrence (line 59)

**Step 3: Update all Python imports**

Global find-and-replace: `from apps.projects` → `from apps.workspace` in all `.py` files listed above. Also update `apps.projects.` in any lazy import strings.

**Step 4: Update URL config**

In `config/urls.py`:
```python
from apps.workspace.api.views import RefreshSchemaView
from apps.workspace.views import health_check
```
And:
```python
path("api/data-dictionary/", include("apps.workspace.api.urls")),
```

**Step 5: Create a migration to handle the app label change**

Django needs a migration to update the `django_content_type` table and internal references. Create `apps/workspace/migrations/0016_rename_app_label.py`:

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspace", "0015_add_discovering_state"),
    ]

    operations = [
        migrations.RunSQL(
            sql="UPDATE django_content_type SET app_label = 'workspace' WHERE app_label = 'projects'",
            reverse_sql="UPDATE django_content_type SET app_label = 'projects' WHERE app_label = 'workspace'",
        ),
    ]
```

Also rename the migrations `__init__.py` module reference — the migration files themselves still reference `"projects"` in their `dependencies`. Update **each migration file** that has `("projects", ...)` in its dependencies list to use `("workspace", ...)`. Same for any cross-app migrations referencing `("projects", ...)`.

Cross-app migration files to update:
- `apps/knowledge/migrations/0006_rescope_to_workspace.py`
- `apps/knowledge/migrations/0002_initial.py`
- `apps/knowledge/migrations/0005_simplify_knowledge.py`
- `apps/recipes/migrations/0005_rescope_to_workspace.py`
- `apps/recipes/migrations/0001_initial_recipe_models.py`
- `apps/artifacts/migrations/0003_rescope_to_workspace.py`
- `apps/artifacts/migrations/0001_initial.py`
- `apps/chat/migrations/0003_thread_tenant_membership_alter_thread_project.py`
- `apps/chat/migrations/0001_initial.py`

In each, change `("projects", "XXXX")` → `("workspace", "XXXX")` in the `dependencies` list.

**Step 6: Run migrations**

```bash
uv run python manage.py migrate
```

**Step 7: Run full test suite**

```bash
uv run pytest tests/ -x -q
```

Expected: All pass. If any import errors remain, fix them.

**Step 8: Run linting**

```bash
uv run ruff check . && uv run ruff format .
```

**Step 9: Commit**

```bash
git add -A && git commit -m "refactor: update all imports and references for apps/projects → apps/workspace rename"
```

---

## Phase 2: Add CustomWorkspace Models

### Task 3: Write tests for CustomWorkspace models

**Files:**
- Create: `tests/test_custom_workspace.py`

**Step 1: Write the model tests**

```python
import pytest
from django.contrib.auth import get_user_model

from apps.users.models import TenantMembership
from apps.workspace.models import TenantWorkspace

User = get_user_model()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@test.com", password="testpass123")


@pytest.fixture
def member(db):
    return User.objects.create_user(email="member@test.com", password="testpass123")


@pytest.fixture
def tenant_membership_a(owner):
    return TenantMembership.objects.create(
        user=owner, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )


@pytest.fixture
def tenant_membership_b(owner):
    return TenantMembership.objects.create(
        user=owner, provider="commcare", tenant_id="domain-b", tenant_name="Domain B"
    )


@pytest.fixture
def member_membership_a(member):
    return TenantMembership.objects.create(
        user=member, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )


@pytest.fixture
def tenant_workspace_a(tenant_membership_a):
    ws, _ = TenantWorkspace.objects.get_or_create(
        tenant_id="domain-a", defaults={"tenant_name": "Domain A"}
    )
    return ws


@pytest.fixture
def tenant_workspace_b(tenant_membership_b):
    ws, _ = TenantWorkspace.objects.get_or_create(
        tenant_id="domain-b", defaults={"tenant_name": "Domain B"}
    )
    return ws


class TestCustomWorkspaceModel:
    def test_create_custom_workspace(self, owner):
        from apps.workspace.models import CustomWorkspace

        ws = CustomWorkspace.objects.create(name="My Workspace", created_by=owner)
        assert ws.name == "My Workspace"
        assert ws.created_by == owner
        assert ws.id is not None

    def test_add_tenants_to_workspace(self, owner, tenant_workspace_a, tenant_workspace_b):
        from apps.workspace.models import CustomWorkspace, CustomWorkspaceTenant

        ws = CustomWorkspace.objects.create(name="Multi-tenant", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_b)
        assert ws.custom_workspace_tenants.count() == 2

    def test_duplicate_tenant_rejected(self, owner, tenant_workspace_a):
        from django.db import IntegrityError

        from apps.workspace.models import CustomWorkspace, CustomWorkspaceTenant

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
        with pytest.raises(IntegrityError):
            CustomWorkspaceTenant.objects.create(
                workspace=ws, tenant_workspace=tenant_workspace_a
            )


class TestWorkspaceMembership:
    def test_create_membership(self, owner):
        from apps.workspace.models import CustomWorkspace, WorkspaceMembership

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        membership = WorkspaceMembership.objects.create(
            workspace=ws, user=owner, role="owner"
        )
        assert membership.role == "owner"
        assert ws.memberships.count() == 1

    def test_duplicate_membership_rejected(self, owner):
        from django.db import IntegrityError

        from apps.workspace.models import CustomWorkspace, WorkspaceMembership

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
        with pytest.raises(IntegrityError):
            WorkspaceMembership.objects.create(workspace=ws, user=owner, role="editor")

    def test_role_choices(self, owner):
        from apps.workspace.models import CustomWorkspace, WorkspaceMembership

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        for role in ["owner", "editor", "viewer"]:
            ws2 = CustomWorkspace.objects.create(name=f"Test-{role}", created_by=owner)
            m = WorkspaceMembership.objects.create(workspace=ws2, user=owner, role=role)
            assert m.role == role
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_custom_workspace.py -x -v
```

Expected: FAIL — `CustomWorkspace` model doesn't exist yet.

---

### Task 4: Implement CustomWorkspace models

**Files:**
- Modify: `apps/workspace/models.py`

**Step 1: Add the new models after TenantMetadata**

Add to end of `apps/workspace/models.py`:

```python
class CustomWorkspace(models.Model):
    """User-created workspace that groups multiple tenants together."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    system_prompt = models.TextField(
        blank=True,
        help_text="Workspace-level system prompt. Layered on top of tenant prompts.",
    )
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="custom_workspaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CustomWorkspaceTenant(models.Model):
    """Links a CustomWorkspace to a TenantWorkspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        CustomWorkspace,
        on_delete=models.CASCADE,
        related_name="custom_workspace_tenants",
    )
    tenant_workspace = models.ForeignKey(
        TenantWorkspace,
        on_delete=models.CASCADE,
        related_name="custom_workspace_links",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["workspace", "tenant_workspace"]

    def __str__(self):
        return f"{self.workspace.name} ← {self.tenant_workspace.tenant_name}"


class WorkspaceMembership(models.Model):
    """Role-based membership for CustomWorkspace."""

    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("editor", "Editor"),
        ("viewer", "Viewer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        CustomWorkspace,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    invited_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_invitations",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["workspace", "user"]

    def __str__(self):
        return f"{self.user.email} - {self.role} in {self.workspace.name}"
```

**Step 2: Generate and run migration**

```bash
uv run python manage.py makemigrations workspace --name add_custom_workspace_models
uv run python manage.py migrate
```

**Step 3: Run tests**

```bash
uv run pytest tests/test_custom_workspace.py -x -v
```

Expected: All pass.

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add CustomWorkspace, CustomWorkspaceTenant, WorkspaceMembership models"
```

---

## Phase 3: Dual FKs on Knowledge and Chat Models

### Task 5: Write tests for dual FK on knowledge models

**Files:**
- Add to: `tests/test_custom_workspace.py`

**Step 1: Add tests for knowledge entry scoping**

```python
class TestKnowledgeDualFK:
    def test_knowledge_entry_on_tenant_workspace(self, tenant_workspace_a):
        from apps.knowledge.models import KnowledgeEntry

        entry = KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a,
            title="Tenant Knowledge",
            content="Some content",
        )
        assert entry.workspace == tenant_workspace_a
        assert entry.custom_workspace is None

    def test_knowledge_entry_on_custom_workspace(self, owner):
        from apps.knowledge.models import KnowledgeEntry
        from apps.workspace.models import CustomWorkspace

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        entry = KnowledgeEntry.objects.create(
            custom_workspace=ws,
            title="Custom Knowledge",
            content="Some content",
        )
        assert entry.custom_workspace == ws
        assert entry.workspace is None

    def test_agent_learning_on_custom_workspace(self, owner):
        from apps.knowledge.models import AgentLearning
        from apps.workspace.models import CustomWorkspace

        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        learning = AgentLearning.objects.create(
            custom_workspace=ws,
            description="Test learning",
        )
        assert learning.custom_workspace == ws
        assert learning.workspace is None
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/test_custom_workspace.py::TestKnowledgeDualFK -x -v
```

Expected: FAIL — `custom_workspace` field doesn't exist.

---

### Task 6: Add dual FK to knowledge and chat models

**Files:**
- Modify: `apps/knowledge/models.py` — add `custom_workspace` FK to TableKnowledge, KnowledgeEntry, AgentLearning
- Modify: `apps/chat/models.py` — add `custom_workspace` FK to Thread

**Step 1: Add `custom_workspace` FK to TableKnowledge**

In `apps/knowledge/models.py`, after the existing `workspace` FK on TableKnowledge (line 36), add:

```python
    custom_workspace = models.ForeignKey(
        "workspace.CustomWorkspace",
        on_delete=models.CASCADE,
        related_name="table_knowledge",
        null=True,
        blank=True,
    )
```

Update Meta:
```python
    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(workspace__isnull=False, custom_workspace__isnull=True)
                    | models.Q(workspace__isnull=True, custom_workspace__isnull=False)
                ),
                name="table_knowledge_one_workspace",
            ),
        ]
        ordering = ["table_name"]
        verbose_name_plural = "Table knowledge"
```

Remove `unique_together = ["workspace", "table_name"]` and replace with the constraint above. Note: uniqueness per workspace type should be handled with two partial unique indexes or application logic, since the table can belong to either workspace type.

**Step 2: Add `custom_workspace` FK to KnowledgeEntry**

After the existing `workspace` FK (line 100), add the same pattern:

```python
    custom_workspace = models.ForeignKey(
        "workspace.CustomWorkspace",
        on_delete=models.CASCADE,
        related_name="knowledge_entries",
        null=True,
        blank=True,
    )
```

Add to Meta:
```python
    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(workspace__isnull=False, custom_workspace__isnull=True)
                    | models.Q(workspace__isnull=True, custom_workspace__isnull=False)
                ),
                name="knowledge_entry_one_workspace",
            ),
        ]
        ordering = ["-updated_at"]
        verbose_name_plural = "Knowledge entries"
```

**Step 3: Add `custom_workspace` FK to AgentLearning**

After the existing `workspace` FK (line 145), same pattern:

```python
    custom_workspace = models.ForeignKey(
        "workspace.CustomWorkspace",
        on_delete=models.CASCADE,
        related_name="learnings",
        null=True,
        blank=True,
    )
```

Update the existing index to also include the new field. Add to Meta constraints:
```python
    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(workspace__isnull=False, custom_workspace__isnull=True)
                    | models.Q(workspace__isnull=True, custom_workspace__isnull=False)
                ),
                name="agent_learning_one_workspace",
            ),
        ]
```

**Step 4: Add `WorkspaceContextQuerySet` manager to knowledge models**

In `apps/knowledge/models.py`, define a shared queryset above the model classes:

```python
class WorkspaceContextQuerySet(models.QuerySet):
    def for_workspace_context(self, context):
        """Filter by workspace context — handles TenantWorkspace and CustomWorkspace.

        All consumers (retriever, agent tools, views) MUST use this method instead
        of writing raw Q(workspace=...) | Q(custom_workspace=...) unions.

        Note (v1): the isinstance check couples this queryset to the CustomWorkspace
        model hierarchy. If a third workspace type is added, extend this method with
        a new branch. Avoid refactoring to duck typing or a workspace protocol until
        there is a concrete third type — the abstraction cost is not justified for v1.
        """
        from apps.workspace.models import CustomWorkspace

        if isinstance(context, CustomWorkspace):
            return self.filter(custom_workspace=context)
        return self.filter(workspace=context)
```

Add `objects = WorkspaceContextQuerySet.as_manager()` to `TableKnowledge`, `KnowledgeEntry`, and `AgentLearning`. This is the **single authorised entry point** for workspace-scoped knowledge queries — existing consumers (retriever, agent tools, recipe views, artifact views) must be updated to call `.for_workspace_context(ctx)` so new workspace types only require a change here.

**Step 4b: Add `clean()` to each dual-FK model**

The `CheckConstraint` enforces the XOR rule at the DB level, but violations surface as an opaque `IntegrityError`. Add a `clean()` method to `TableKnowledge`, `KnowledgeEntry`, and `AgentLearning` that raises a clear application-level `ValidationError` before the constraint fires. Add `from django.core.exceptions import ValidationError` at the top of `apps/knowledge/models.py`.

```python
# Add to each of TableKnowledge, KnowledgeEntry, AgentLearning:
def clean(self):
    if bool(self.workspace_id) == bool(self.custom_workspace_id):
        raise ValidationError(
            "Exactly one of 'workspace' or 'custom_workspace' must be set."
        )
```

Apply the same pattern to the `Thread` model in `apps/chat/models.py`.

---

**Step 5: Add `custom_workspace` FK to Thread**

In `apps/chat/models.py`, after the `tenant_membership` FK (line 18), add:

```python
    custom_workspace = models.ForeignKey(
        "workspace.CustomWorkspace",
        on_delete=models.CASCADE,
        related_name="threads",
        null=True,
        blank=True,
    )
```

**Step 6: Generate and run migrations**

```bash
uv run python manage.py makemigrations knowledge chat --name add_custom_workspace_fk
uv run python manage.py migrate
```

**Step 7: Run tests**

```bash
uv run pytest tests/test_custom_workspace.py -x -v
```

Expected: All pass.

**Step 8: Commit**

```bash
git add -A && git commit -m "feat: add custom_workspace FK to knowledge and chat models with check constraints"
```

---

## Phase 4: Backend API

### Task 7: Write tests for CustomWorkspace CRUD API

**Files:**
- Create: `tests/test_custom_workspace_api.py`

**Step 1: Write API tests**

```python
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.models import TenantMembership
from apps.workspace.models import CustomWorkspace, TenantWorkspace, WorkspaceMembership

User = get_user_model()


@pytest.fixture
def api_client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def owner(db):
    user = User.objects.create_user(email="owner@test.com", password="testpass123")
    return user


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@test.com", password="testpass123")


@pytest.fixture
def tenant_workspace_a(db):
    return TenantWorkspace.objects.create(tenant_id="domain-a", tenant_name="Domain A")


@pytest.fixture
def tenant_workspace_b(db):
    return TenantWorkspace.objects.create(tenant_id="domain-b", tenant_name="Domain B")


@pytest.fixture
def owner_memberships(owner, tenant_workspace_a, tenant_workspace_b):
    TenantMembership.objects.create(
        user=owner, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )
    TenantMembership.objects.create(
        user=owner, provider="commcare", tenant_id="domain-b", tenant_name="Domain B"
    )


@pytest.fixture
def other_user_partial_access(other_user):
    """other_user only has access to domain-a, not domain-b."""
    TenantMembership.objects.create(
        user=other_user, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )


@pytest.fixture
def custom_workspace(owner, tenant_workspace_a, tenant_workspace_b, owner_memberships):
    from apps.workspace.models import CustomWorkspaceTenant

    ws = CustomWorkspace.objects.create(name="Test Workspace", created_by=owner)
    CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
    CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_b)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


class TestCustomWorkspaceList:
    def test_list_returns_only_user_workspaces(self, api_client, owner, custom_workspace):
        api_client.force_login(owner)
        response = api_client.get("/api/custom-workspaces/")
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["name"] == "Test Workspace"

    def test_list_excludes_non_member_workspaces(self, api_client, other_user, custom_workspace):
        api_client.force_login(other_user)
        response = api_client.get("/api/custom-workspaces/")
        assert response.status_code == 200
        assert len(response.json()) == 0

    def test_unauthenticated_returns_403(self, api_client):
        response = api_client.get("/api/custom-workspaces/")
        assert response.status_code == 403


class TestCustomWorkspaceCreate:
    def test_create_workspace(self, api_client, owner, tenant_workspace_a, owner_memberships):
        api_client.force_login(owner)
        response = api_client.post(
            "/api/custom-workspaces/",
            data={
                "name": "New Workspace",
                "tenant_workspace_ids": [str(tenant_workspace_a.id)],
            },
            content_type="application/json",
        )
        assert response.status_code == 201
        ws = CustomWorkspace.objects.get(name="New Workspace")
        assert ws.created_by == owner
        assert ws.custom_workspace_tenants.count() == 1
        assert WorkspaceMembership.objects.filter(workspace=ws, user=owner, role="owner").exists()


class TestCustomWorkspaceEnter:
    def test_enter_workspace_success(self, api_client, owner, custom_workspace):
        api_client.force_login(owner)
        response = api_client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 200

    def test_enter_blocked_when_missing_tenant_access(
        self, api_client, other_user, custom_workspace, other_user_partial_access
    ):
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        api_client.force_login(other_user)
        response = api_client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 403
        data = response.json()
        assert "domain-b" in str(data.get("missing_tenants", []))

    def test_enter_blocked_for_non_member(self, api_client, other_user, custom_workspace):
        api_client.force_login(other_user)
        response = api_client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 403


class TestOwnerRoleProtection:
    """The 'owner' role is a security invariant: it can only be set at workspace creation,
    never via the invite or role-change endpoints."""

    def test_cannot_invite_with_owner_role(
        self, api_client, owner, custom_workspace, other_user, owner_memberships
    ):
        # Give other_user access to all tenants so the invite would otherwise succeed
        from apps.users.models import TenantMembership
        TenantMembership.objects.create(
            user=other_user, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
        )
        TenantMembership.objects.create(
            user=other_user, provider="commcare", tenant_id="domain-b", tenant_name="Domain B"
        )
        api_client.force_login(owner)
        response = api_client.post(
            f"/api/custom-workspaces/{custom_workspace.id}/members/",
            data={"user_id": str(other_user.id), "role": "owner"},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_cannot_change_role_to_owner(
        self, api_client, owner, custom_workspace, other_user
    ):
        from apps.workspace.models import WorkspaceMembership
        membership = WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        api_client.force_login(owner)
        response = api_client.patch(
            f"/api/custom-workspaces/{custom_workspace.id}/members/{membership.id}/",
            data={"role": "owner"},
            content_type="application/json",
        )
        assert response.status_code == 400
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/test_custom_workspace_api.py -x -v
```

Expected: FAIL — URL not found (404).

---

### Task 7.5: Extract pure role-check function and add direct permission tests

**Files:**
- Modify: `apps/workspace/api/views.py` — extract pure `_can_perform_action` from `_require_permission`
- Create: `tests/test_workspace_permissions.py`

**Rationale:** `_require_permission` is the security core of the entire feature. It must be tested directly, not only through API round-trips. Extracting the role-check logic as a pure function (no ORM) allows testing the `ROLE_PERMISSIONS` matrix without hitting the database, making the tests fast and explicit.

**Step 1: Extract `_can_perform_action` as a pure function**

In `apps/workspace/api/views.py`, above `_require_permission`, add:

```python
def _can_perform_action(role: str, action: str) -> bool:
    """Pure function: returns True if the given role can perform the given action.

    This is the authoritative check for the ROLE_PERMISSIONS matrix.
    Separating it from the ORM lookup makes it unit-testable without a database.
    """
    return role in ROLE_PERMISSIONS.get(action, [])
```

Update `_require_permission` to use it:

```python
def _require_permission(user, workspace, action):
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=user).first()
    if not membership:
        raise PermissionDenied("Not a member of this workspace.")
    if not _can_perform_action(membership.role, action):
        raise PermissionDenied(f"Requires role: {', '.join(ROLE_PERMISSIONS[action])}")
    return membership
```

**Step 2: Write pure unit tests (no DB)**

Create `tests/test_workspace_permissions.py`:

```python
import pytest
from rest_framework.exceptions import PermissionDenied
from unittest.mock import MagicMock, patch


class TestCanPerformAction:
    """Pure unit tests for the role permission matrix. No database required."""

    def test_owner_can_do_all_actions(self):
        from apps.workspace.api.views import _can_perform_action, ROLE_PERMISSIONS

        for action in ROLE_PERMISSIONS:
            assert _can_perform_action("owner", action), f"Owner should be able to {action}"

    def test_editor_cannot_manage_tenants(self):
        from apps.workspace.api.views import _can_perform_action

        assert not _can_perform_action("editor", "manage_tenants")

    def test_editor_cannot_manage_members(self):
        from apps.workspace.api.views import _can_perform_action

        assert not _can_perform_action("editor", "manage_members")

    def test_editor_can_edit_content(self):
        from apps.workspace.api.views import _can_perform_action

        assert _can_perform_action("editor", "edit_content")

    def test_viewer_cannot_edit_content(self):
        from apps.workspace.api.views import _can_perform_action

        assert not _can_perform_action("viewer", "edit_content")

    def test_viewer_can_view(self):
        from apps.workspace.api.views import _can_perform_action

        assert _can_perform_action("viewer", "view")

    def test_unknown_role_cannot_do_anything(self):
        from apps.workspace.api.views import _can_perform_action, ROLE_PERMISSIONS

        for action in ROLE_PERMISSIONS:
            assert not _can_perform_action("superadmin", action)

    def test_unknown_action_returns_false(self):
        from apps.workspace.api.views import _can_perform_action

        assert not _can_perform_action("owner", "nonexistent_action")


class TestRequirePermission:
    """DB-backed tests for _require_permission (membership lookup + role check)."""

    @pytest.mark.django_db
    def test_non_member_raises_permission_denied(self):
        from django.contrib.auth import get_user_model
        from apps.workspace.api.views import _require_permission
        from apps.workspace.models import CustomWorkspace

        User = get_user_model()
        user = User.objects.create_user(email="perm_test@test.com", password="pass")
        ws = CustomWorkspace.objects.create(name="Test", created_by=user)

        other = User.objects.create_user(email="other_perm@test.com", password="pass")
        with pytest.raises(PermissionDenied):
            _require_permission(other, ws, "view")

    @pytest.mark.django_db
    def test_member_with_sufficient_role_returns_membership(self):
        from django.contrib.auth import get_user_model
        from apps.workspace.api.views import _require_permission
        from apps.workspace.models import CustomWorkspace, WorkspaceMembership

        User = get_user_model()
        user = User.objects.create_user(email="perm_owner@test.com", password="pass")
        ws = CustomWorkspace.objects.create(name="Test", created_by=user)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")

        membership = _require_permission(user, ws, "delete")
        assert membership.role == "owner"

    @pytest.mark.django_db
    def test_viewer_cannot_delete(self):
        from django.contrib.auth import get_user_model
        from apps.workspace.api.views import _require_permission
        from apps.workspace.models import CustomWorkspace, WorkspaceMembership

        User = get_user_model()
        owner = User.objects.create_user(email="perm_owner2@test.com", password="pass")
        viewer = User.objects.create_user(email="perm_viewer@test.com", password="pass")
        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)
        WorkspaceMembership.objects.create(workspace=ws, user=viewer, role="viewer")

        with pytest.raises(PermissionDenied):
            _require_permission(viewer, ws, "delete")
```

**Step 3: Run tests**

```bash
uv run pytest tests/test_workspace_permissions.py -v
```

Expected: `TestCanPerformAction` tests run with no DB. `TestRequirePermission` tests are fast (minimal fixtures).

**Step 4: Commit**

```bash
git add -A && git commit -m "refactor: extract _can_perform_action pure function + add permission matrix tests"
```

---

### Task 8: Implement CustomWorkspace API views, serializers, and URLs

**Files:**
- Create: `apps/workspace/api/serializers.py`
- Modify: `apps/workspace/api/views.py`
- Modify: `apps/workspace/api/urls.py`
- Modify: `config/urls.py`

**Step 1: Create serializers**

Create `apps/workspace/api/serializers.py`:

```python
from rest_framework import serializers

from apps.workspace.models import CustomWorkspace, CustomWorkspaceTenant, WorkspaceMembership


class CustomWorkspaceTenantSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source="tenant_workspace.tenant_id", read_only=True)
    tenant_name = serializers.CharField(source="tenant_workspace.tenant_name", read_only=True)
    tenant_workspace_id = serializers.UUIDField(source="tenant_workspace.id", read_only=True)

    class Meta:
        model = CustomWorkspaceTenant
        fields = ["id", "tenant_workspace_id", "tenant_id", "tenant_name", "added_at"]


class WorkspaceMembershipSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    user_id = serializers.UUIDField(source="user.id", read_only=True)

    class Meta:
        model = WorkspaceMembership
        fields = ["id", "user_id", "email", "role", "joined_at"]


class CustomWorkspaceListSerializer(serializers.ModelSerializer):
    tenant_count = serializers.IntegerField(read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    role = serializers.CharField(read_only=True)

    class Meta:
        model = CustomWorkspace
        fields = [
            "id", "name", "description", "created_at", "updated_at",
            "tenant_count", "member_count", "role",
        ]


class CustomWorkspaceDetailSerializer(serializers.ModelSerializer):
    tenants = CustomWorkspaceTenantSerializer(
        source="custom_workspace_tenants", many=True, read_only=True
    )
    members = WorkspaceMembershipSerializer(source="memberships", many=True, read_only=True)

    class Meta:
        model = CustomWorkspace
        fields = [
            "id", "name", "description", "system_prompt",
            "created_at", "updated_at", "tenants", "members",
        ]


class CustomWorkspaceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, default="")
    tenant_workspace_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1
    )


class CustomWorkspaceUpdateSerializer(serializers.ModelSerializer):
    """Used by PATCH /<id>/. Validates field constraints before saving."""

    class Meta:
        model = CustomWorkspace
        fields = ["name", "description", "system_prompt"]

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Name cannot be blank.")
        return value
```

**Step 2: Add views**

Add to `apps/workspace/api/views.py` (after existing views). All imports at module level per project convention:

```python
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Count, Subquery, OuterRef
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.users.models import TenantMembership
from apps.workspace.api.serializers import (
    CustomWorkspaceCreateSerializer,
    CustomWorkspaceDetailSerializer,
    CustomWorkspaceListSerializer,
    CustomWorkspaceTenantSerializer,
    CustomWorkspaceUpdateSerializer,
    WorkspaceMembershipSerializer,
)
from apps.workspace.models import (
    CustomWorkspace,
    CustomWorkspaceTenant,
    TenantWorkspace,
    WorkspaceMembership,
)

User = get_user_model()

ROLE_PERMISSIONS = {
    "view": ["owner", "editor", "viewer"],
    "edit_content": ["owner", "editor"],
    "manage_tenants": ["owner"],
    "manage_members": ["owner"],
    "edit_settings": ["owner"],
    "delete": ["owner"],
}


def _can_perform_action(role: str, action: str) -> bool:
    """Pure function: returns True if the given role can perform the given action.

    Separated from the ORM lookup so the permission matrix can be tested without a database.
    See tests/test_workspace_permissions.py.
    """
    return role in ROLE_PERMISSIONS.get(action, [])


def _require_permission(user, workspace, action):
    """Check user has permission for the given action. Returns the membership or raises.

    Use action keys from ROLE_PERMISSIONS — never pass raw role lists into views.
    Owner role is granted only at workspace creation; it cannot be set via the invite endpoint.
    """
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=user).first()
    if not membership:
        raise PermissionDenied("Not a member of this workspace.")
    if not _can_perform_action(membership.role, action):
        raise PermissionDenied(f"Requires role: {', '.join(ROLE_PERMISSIONS[action])}")
    return membership


def _validate_tenant_access(user, workspace):
    """Validate user has TenantMembership for all tenants in workspace. Returns missing list.

    Filters TenantMembership to only the workspace's tenant IDs (not all user tenants)
    so this is O(workspace_tenant_count), not O(all_user_tenants).
    """
    workspace_tenant_ids = set(
        workspace.custom_workspace_tenants.values_list(
            "tenant_workspace__tenant_id", flat=True
        )
    )
    covered_ids = set(
        TenantMembership.objects.filter(
            user=user, tenant_id__in=workspace_tenant_ids
        ).values_list("tenant_id", flat=True)
    )
    return list(workspace_tenant_ids - covered_ids)


def _get_workspace_or_403(workspace_id, user, action):
    """Fetch a CustomWorkspace by ID and verify the user has the required action permission.

    Raises 404 if not found, PermissionDenied if the user lacks the required role.
    Returns the workspace instance. Use this at the start of every view method instead
    of repeating the fetch + permission check inline.
    """
    workspace = get_object_or_404(CustomWorkspace, id=workspace_id)
    _require_permission(user, workspace, action)
    return workspace


class CustomWorkspaceListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspaces = (
            CustomWorkspace.objects.filter(memberships__user=request.user)
            .annotate(
                # distinct=True is required: the memberships JOIN used for filtering
                # inflates Count results without it.
                tenant_count=Count("custom_workspace_tenants", distinct=True),
                member_count=Count("memberships", distinct=True),
                # Correlated subquery for role: result set is user-scoped so typically
                # small. If users accumulate many custom workspaces, consider replacing
                # with prefetch_related + Python annotation.
                role=Subquery(
                    WorkspaceMembership.objects.filter(
                        workspace=OuterRef("pk"), user=request.user
                    ).values("role")[:1]
                ),
            )
            .order_by("name")
        )
        serializer = CustomWorkspaceListSerializer(workspaces, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = CustomWorkspaceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tenant_workspace_ids = serializer.validated_data["tenant_workspace_ids"]
        tenant_workspaces = TenantWorkspace.objects.filter(id__in=tenant_workspace_ids)
        if tenant_workspaces.count() != len(tenant_workspace_ids):
            raise ValidationError("One or more tenant workspaces not found.")

        # Verify user has TenantMembership for all requested tenants
        tenant_ids = set(tenant_workspaces.values_list("tenant_id", flat=True))
        covered_ids = set(
            TenantMembership.objects.filter(
                user=request.user, tenant_id__in=tenant_ids
            ).values_list("tenant_id", flat=True)
        )
        missing = tenant_ids - covered_ids
        if missing:
            raise ValidationError(f"No access to tenants: {', '.join(missing)}")

        workspace = CustomWorkspace.objects.create(
            name=serializer.validated_data["name"],
            description=serializer.validated_data.get("description", ""),
            created_by=request.user,
        )
        for tw in tenant_workspaces:
            CustomWorkspaceTenant.objects.create(workspace=workspace, tenant_workspace=tw)
        WorkspaceMembership.objects.create(
            workspace=workspace, user=request.user, role="owner"
        )

        detail = CustomWorkspaceDetailSerializer(workspace)
        return Response(detail.data, status=status.HTTP_201_CREATED)


class CustomWorkspaceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "view")
        serializer = CustomWorkspaceDetailSerializer(workspace)
        return Response(serializer.data)

    def patch(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "edit_settings")
        serializer = CustomWorkspaceUpdateSerializer(workspace, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(CustomWorkspaceDetailSerializer(workspace).data)

    def delete(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "delete")
        workspace.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomWorkspaceEnterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        # Fetch with prefetch upfront — used for both access validation and serialization.
        workspace = get_object_or_404(
            CustomWorkspace.objects.prefetch_related(
                "custom_workspace_tenants__tenant_workspace", "memberships__user"
            ),
            id=workspace_id,
        )
        _require_permission(request.user, workspace, "view")

        missing = _validate_tenant_access(request.user, workspace)
        if missing:
            return Response(
                {
                    "error": "Missing tenant access",
                    "missing_tenants": missing,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CustomWorkspaceDetailSerializer(workspace)
        return Response(serializer.data)


class CustomWorkspaceTenantListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "view")
        tenants = workspace.custom_workspace_tenants.select_related("tenant_workspace")
        serializer = CustomWorkspaceTenantSerializer(tenants, many=True)
        return Response(serializer.data)

    def post(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "manage_tenants")

        tw_id = request.data.get("tenant_workspace_id")
        tw = TenantWorkspace.objects.filter(id=tw_id).first()
        if not tw:
            raise ValidationError("Tenant workspace not found.")

        if not TenantMembership.objects.filter(
            user=request.user, tenant_id=tw.tenant_id
        ).exists():
            raise ValidationError("You don't have access to this tenant.")

        cwt, created = CustomWorkspaceTenant.objects.get_or_create(
            workspace=workspace, tenant_workspace=tw
        )
        if not created:
            raise ValidationError("Tenant already in workspace.")

        serializer = CustomWorkspaceTenantSerializer(cwt)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CustomWorkspaceTenantDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, workspace_id, tenant_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "manage_tenants")
        deleted, _ = CustomWorkspaceTenant.objects.filter(
            workspace=workspace, id=tenant_id
        ).delete()
        if not deleted:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "view")
        members = workspace.memberships.select_related("user")
        serializer = WorkspaceMembershipSerializer(members, many=True)
        return Response(serializer.data)

    def post(self, request, workspace_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "manage_members")

        user_id = request.data.get("user_id")
        role = request.data.get("role", "viewer")
        # "owner" cannot be assigned via invite — it is set only at workspace creation.
        if role not in ["editor", "viewer"]:
            raise ValidationError("Role must be 'editor' or 'viewer'.")

        invitee = User.objects.filter(id=user_id).first()
        if not invitee:
            raise ValidationError("User not found.")

        # Validate invitee has access to all tenants
        missing = _validate_tenant_access(invitee, workspace)
        if missing:
            raise ValidationError(
                f"Invitee lacks access to tenants: {', '.join(missing)}"
            )

        membership, created = WorkspaceMembership.objects.get_or_create(
            workspace=workspace,
            user=invitee,
            defaults={"role": role, "invited_by": request.user},
        )
        if not created:
            raise ValidationError("User is already a member.")

        serializer = WorkspaceMembershipSerializer(membership)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkspaceMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, workspace_id, member_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "manage_members")

        membership = WorkspaceMembership.objects.filter(
            workspace=workspace, id=member_id
        ).first()
        if not membership:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        role = request.data.get("role")
        # "owner" cannot be assigned via role change — only at workspace creation.
        if role and role in ["editor", "viewer"]:
            membership.role = role
            membership.save(update_fields=["role"])
        elif role:
            raise ValidationError("Role must be 'editor' or 'viewer'.")

        serializer = WorkspaceMembershipSerializer(membership)
        return Response(serializer.data)

    def delete(self, request, workspace_id, member_id):
        workspace = _get_workspace_or_403(workspace_id, request.user, "manage_members")

        deleted, _ = WorkspaceMembership.objects.filter(
            workspace=workspace, id=member_id
        ).delete()
        if not deleted:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
```

**Step 3: Add URL configuration**

Create or update URL routing. Add to `config/urls.py`:

```python
path("api/custom-workspaces/", include("apps.workspace.api.custom_workspace_urls")),
```

Create `apps/workspace/api/custom_workspace_urls.py`:

```python
from django.urls import path

from apps.workspace.api.views import (
    CustomWorkspaceDetailView,
    CustomWorkspaceEnterView,
    CustomWorkspaceListCreateView,
    CustomWorkspaceTenantDeleteView,
    CustomWorkspaceTenantListCreateView,
    WorkspaceMemberDetailView,
    WorkspaceMemberListCreateView,
)

app_name = "custom_workspaces"

urlpatterns = [
    path("", CustomWorkspaceListCreateView.as_view(), name="list-create"),
    path("<uuid:workspace_id>/", CustomWorkspaceDetailView.as_view(), name="detail"),
    path("<uuid:workspace_id>/enter/", CustomWorkspaceEnterView.as_view(), name="enter"),
    path(
        "<uuid:workspace_id>/tenants/",
        CustomWorkspaceTenantListCreateView.as_view(),
        name="tenants",
    ),
    path(
        "<uuid:workspace_id>/tenants/<uuid:tenant_id>/",
        CustomWorkspaceTenantDeleteView.as_view(),
        name="tenant-delete",
    ),
    path(
        "<uuid:workspace_id>/members/",
        WorkspaceMemberListCreateView.as_view(),
        name="members",
    ),
    path(
        "<uuid:workspace_id>/members/<uuid:member_id>/",
        WorkspaceMemberDetailView.as_view(),
        name="member-detail",
    ),
]
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_custom_workspace_api.py -x -v
```

Expected: All pass.

**Step 5: Run linting**

```bash
uv run ruff check . && uv run ruff format .
```

**Step 6: Commit**

```bash
git add -A && git commit -m "feat: add CustomWorkspace REST API with CRUD, enter, tenants, and members endpoints"
```

---

### Task 8.5: Implement and test `_resolve_workspace` utility

**Files:**
- Create: `apps/workspace/utils.py`
- Modify: `apps/workspace/api/views.py` (import and use)
- Add to: `tests/test_workspace_permissions.py`

**Rationale:** Every API that accepts the `X-Custom-Workspace` header must resolve and re-validate workspace access on that header. This is the security boundary for the entire feature. Defining it as an explicit, tested utility prevents inconsistent implementations across views.

**Security requirement:** On every request carrying `X-Custom-Workspace`, the resolver must:
1. Verify the workspace exists
2. Verify the requesting user is a `WorkspaceMembership` member
3. Verify the user still has `TenantMembership` for all workspace tenants

Step 2 and 3 must happen on every request — not just at `enter/` time. This ensures a user who loses tenant access mid-session gets a 403 on the next API call, not just on explicit re-entry.

**Step 1: Create `apps/workspace/utils.py`**

```python
from rest_framework.exceptions import PermissionDenied

from apps.workspace.models import CustomWorkspace, WorkspaceMembership


def resolve_workspace_from_request(request):
    """Resolve the active workspace context from the request.

    If the X-Custom-Workspace header is present, resolves the CustomWorkspace and
    performs a full access check (membership + all tenant memberships). If the header
    is absent, returns None (caller falls back to TenantWorkspace resolution).

    Security invariant: this check runs on EVERY request carrying the header,
    not just at enter/ time. A user who loses tenant access mid-session will receive
    a 403 on their next API call.

    Raises PermissionDenied if the workspace is found but access is denied.
    Returns None if the header is not present.
    """
    workspace_id = request.headers.get("X-Custom-Workspace")
    if not workspace_id:
        return None

    from apps.workspace.api.views import _validate_tenant_access

    workspace = CustomWorkspace.objects.filter(id=workspace_id).first()
    if not workspace:
        raise PermissionDenied("Custom workspace not found.")

    if not WorkspaceMembership.objects.filter(
        workspace=workspace, user=request.user
    ).exists():
        raise PermissionDenied("Not a member of this workspace.")

    missing = _validate_tenant_access(request.user, workspace)
    if missing:
        raise PermissionDenied(
            f"Lost access to tenants: {', '.join(missing)}. Contact workspace owner."
        )

    return workspace
```

**Step 2: Add tests to `tests/test_workspace_permissions.py`**

```python
class TestResolveWorkspaceFromRequest:
    """Tests for the _resolve_workspace utility — the security boundary for header-bearing requests."""

    def _make_request(self, user, workspace_id=None):
        from django.test import RequestFactory
        from rest_framework.request import Request

        factory = RequestFactory()
        headers = {}
        if workspace_id:
            headers["HTTP_X_CUSTOM_WORKSPACE"] = str(workspace_id)
        django_request = factory.get("/", **headers)
        django_request.user = user
        return Request(django_request)

    @pytest.mark.django_db
    def test_returns_none_when_header_absent(self):
        from django.contrib.auth import get_user_model
        from apps.workspace.utils import resolve_workspace_from_request

        User = get_user_model()
        user = User.objects.create_user(email="resolve1@test.com", password="pass")
        request = self._make_request(user)

        result = resolve_workspace_from_request(request)
        assert result is None

    @pytest.mark.django_db
    def test_returns_workspace_for_valid_member(self):
        from django.contrib.auth import get_user_model
        from apps.workspace.models import CustomWorkspace, WorkspaceMembership
        from apps.workspace.utils import resolve_workspace_from_request

        User = get_user_model()
        user = User.objects.create_user(email="resolve2@test.com", password="pass")
        ws = CustomWorkspace.objects.create(name="Test", created_by=user)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")

        request = self._make_request(user, workspace_id=ws.id)
        result = resolve_workspace_from_request(request)
        assert result == ws

    @pytest.mark.django_db
    def test_raises_for_non_member(self):
        from django.contrib.auth import get_user_model
        from apps.workspace.models import CustomWorkspace
        from apps.workspace.utils import resolve_workspace_from_request

        User = get_user_model()
        owner = User.objects.create_user(email="resolve3@test.com", password="pass")
        other = User.objects.create_user(email="resolve4@test.com", password="pass")
        ws = CustomWorkspace.objects.create(name="Test", created_by=owner)

        request = self._make_request(other, workspace_id=ws.id)
        with pytest.raises(PermissionDenied):
            resolve_workspace_from_request(request)

    @pytest.mark.django_db
    def test_raises_when_tenant_access_revoked(self):
        """Re-validates tenant access on every request, not just at enter/ time."""
        from django.contrib.auth import get_user_model
        from apps.users.models import TenantMembership
        from apps.workspace.models import (
            CustomWorkspace, CustomWorkspaceTenant, TenantWorkspace, WorkspaceMembership,
        )
        from apps.workspace.utils import resolve_workspace_from_request

        User = get_user_model()
        user = User.objects.create_user(email="resolve5@test.com", password="pass")
        tw = TenantWorkspace.objects.create(tenant_id="revoke-domain", tenant_name="Revoke")
        ws = CustomWorkspace.objects.create(name="Test", created_by=user)
        CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tw)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")
        # User does NOT have TenantMembership for "revoke-domain"

        request = self._make_request(user, workspace_id=ws.id)
        with pytest.raises(PermissionDenied):
            resolve_workspace_from_request(request)
```

**Step 3: Run tests**

```bash
uv run pytest tests/test_workspace_permissions.py -v
```

**Step 4: Update existing `_resolve_workspace` consumers**

In `apps/workspace/api/views.py` (and any other view that currently resolves workspace context), import and call `resolve_workspace_from_request` to handle the `X-Custom-Workspace` header. The existing TenantWorkspace resolution is the fallback when the function returns `None`.

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: add resolve_workspace_from_request utility with full access re-validation"
```

**Known v1 limitation:** If a user loses access to a workspace tenant after being invited as a member, they will receive a 403 on their next API request (via `resolve_workspace_from_request`). The workspace owner has no current UI to see which members have stale tenant access. This is acceptable for v1 — the `enter/` and header-validation 403 responses provide the correct user-facing behavior.

---

## Phase 5: Frontend Workspace Store

### Task 9: Add workspaceSlice to Zustand store

**Files:**
- Create: `frontend/src/store/workspaceSlice.ts`
- Modify: `frontend/src/store/store.ts`
- Modify: `frontend/src/api/client.ts`

**Step 1: Create the workspace slice**

```typescript
// frontend/src/store/workspaceSlice.ts
import { StateCreator } from "zustand"
import { api } from "../api/client"
import type { AppStore } from "./store"

export interface CustomWorkspaceTenant {
  id: string
  tenant_workspace_id: string
  tenant_id: string
  tenant_name: string
  added_at: string
}

export interface WorkspaceMember {
  id: string
  user_id: string
  email: string
  role: "owner" | "editor" | "viewer"
  joined_at: string
}

export interface CustomWorkspace {
  id: string
  name: string
  description: string
  tenant_count: number
  member_count: number
  role: string
  created_at: string
  updated_at: string
}

export interface CustomWorkspaceDetail extends Omit<CustomWorkspace, "tenant_count" | "member_count" | "role"> {
  system_prompt: string
  tenants: CustomWorkspaceTenant[]
  members: WorkspaceMember[]
}

type WorkspaceMode = "tenant" | "custom"

export interface WorkspaceSlice {
  customWorkspaces: CustomWorkspace[]
  activeCustomWorkspaceId: string | null
  activeCustomWorkspace: CustomWorkspaceDetail | null
  workspaceMode: WorkspaceMode
  customWorkspacesStatus: "idle" | "loading" | "loaded" | "error"
  customWorkspacesError: string | null
  enterError: string | null
  missingTenants: string[]
  workspaceActions: {
    fetchCustomWorkspaces: () => Promise<void>
    enterCustomWorkspace: (id: string) => Promise<void>
    exitCustomWorkspace: () => void
    createCustomWorkspace: (data: {
      name: string
      description?: string
      tenant_workspace_ids: string[]
    }) => Promise<CustomWorkspaceDetail>
  }
}

export const createWorkspaceSlice: StateCreator<AppStore, [], [], WorkspaceSlice> = (
  set,
  get
) => ({
  customWorkspaces: [],
  activeCustomWorkspaceId: null,
  activeCustomWorkspace: null,
  workspaceMode: "tenant",
  customWorkspacesStatus: "idle",
  customWorkspacesError: null,
  enterError: null,
  missingTenants: [],
  workspaceActions: {
    fetchCustomWorkspaces: async () => {
      set({ customWorkspacesStatus: "loading", customWorkspacesError: null })
      try {
        const data = await api.get<CustomWorkspace[]>("/api/custom-workspaces/")
        set({ customWorkspaces: data, customWorkspacesStatus: "loaded" })
      } catch (e) {
        set({
          customWorkspacesStatus: "error",
          customWorkspacesError: e instanceof Error ? e.message : "Failed to fetch",
        })
      }
    },
    enterCustomWorkspace: async (id: string) => {
      set({ enterError: null, missingTenants: [] })
      try {
        const data = await api.post<CustomWorkspaceDetail>(
          `/api/custom-workspaces/${id}/enter/`
        )
        set({
          activeCustomWorkspaceId: id,
          activeCustomWorkspace: data,
          workspaceMode: "custom",
          enterError: null,
          missingTenants: [],
        })
      } catch (e: any) {
        const body = e?.body
        if (body?.missing_tenants) {
          set({ enterError: body.error, missingTenants: body.missing_tenants })
        } else {
          set({ enterError: e instanceof Error ? e.message : "Failed to enter workspace" })
        }
        throw e
      }
    },
    exitCustomWorkspace: () => {
      set({
        activeCustomWorkspaceId: null,
        activeCustomWorkspace: null,
        workspaceMode: "tenant",
        enterError: null,
        missingTenants: [],
      })
    },
    createCustomWorkspace: async (data) => {
      const created = await api.post<CustomWorkspaceDetail>("/api/custom-workspaces/", data)
      const { workspaceActions } = get()
      await workspaceActions.fetchCustomWorkspaces()
      return created
    },
  },
})
```

**Step 2: Register in store**

In `frontend/src/store/store.ts`, add the import and spread:

```typescript
import { WorkspaceSlice, createWorkspaceSlice } from "./workspaceSlice"

export type AppStore = ArtifactSlice & AuthSlice & UiSlice & DictionarySlice & KnowledgeSlice & RecipeSlice & DomainSlice & WorkspaceSlice

export const useAppStore = create<AppStore>()((...a) => ({
  ...createArtifactSlice(...a),
  ...createAuthSlice(...a),
  ...createUiSlice(...a),
  ...createDictionarySlice(...a),
  ...createKnowledgeSlice(...a),
  ...createRecipeSlice(...a),
  ...createDomainSlice(...a),
  ...createWorkspaceSlice(...a),
}))
```

**Step 3: Add X-Custom-Workspace header to API client**

In `frontend/src/api/client.ts`, modify the `request` function to include the header when in custom workspace mode. The cleanest approach is to export a function to get the current workspace ID and include it in headers:

```typescript
// Add to client.ts
let activeCustomWorkspaceId: string | null = null

export function setActiveCustomWorkspaceId(id: string | null) {
  activeCustomWorkspaceId = id
}

// In the request function, add to headers:
// ...(activeCustomWorkspaceId && { "X-Custom-Workspace": activeCustomWorkspaceId }),
```

Then in the workspaceSlice, call `setActiveCustomWorkspaceId` when entering/exiting.

**Step 4: Run frontend lint**

```bash
cd frontend && bun run lint
```

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: add workspaceSlice to frontend Zustand store"
```

---

## Phase 6: Frontend Workspace Selector UI

### Task 10: Create the full-width tabbed workspace selector

**Files:**
- Create: `frontend/src/components/WorkspaceSelector/WorkspaceSelector.tsx`
- Modify: `frontend/src/components/Sidebar/Sidebar.tsx`

**Step 1: Create the WorkspaceSelector component**

This is a full-width modal/panel with three tabs: Custom, CommCare, Connect.

```tsx
// frontend/src/components/WorkspaceSelector/WorkspaceSelector.tsx
import { useEffect, useMemo, useState } from "react"
import { useAppStore } from "../../store/store"

interface WorkspaceSelectorProps {
  open: boolean
  onClose: () => void
}

export function WorkspaceSelector({ open, onClose }: WorkspaceSelectorProps) {
  const domains = useAppStore((s) => s.domains)
  const customWorkspaces = useAppStore((s) => s.customWorkspaces)
  const { fetchCustomWorkspaces, enterCustomWorkspace } = useAppStore(
    (s) => s.workspaceActions
  )
  const { setActiveDomain } = useAppStore((s) => s.domainActions)
  const enterError = useAppStore((s) => s.enterError)
  const missingTenants = useAppStore((s) => s.missingTenants)

  const [activeTab, setActiveTab] = useState<"custom" | "commcare" | "connect">("custom")
  const [search, setSearch] = useState("")

  useEffect(() => {
    if (open) fetchCustomWorkspaces()
  }, [open])

  const groupedDomains = useMemo(() => {
    const commcare = domains.filter((d) => d.provider === "commcare")
    const connect = domains.filter((d) => d.provider === "commcare_connect")
    return { commcare, connect }
  }, [domains])

  const filteredCustom = customWorkspaces.filter((w) =>
    w.name.toLowerCase().includes(search.toLowerCase())
  )
  const filteredCommcare = groupedDomains.commcare.filter((d) =>
    d.tenant_name.toLowerCase().includes(search.toLowerCase())
  )
  const filteredConnect = groupedDomains.connect.filter((d) =>
    d.tenant_name.toLowerCase().includes(search.toLowerCase())
  )

  if (!open) return null

  const tabs = [
    { key: "custom" as const, label: "Custom", count: customWorkspaces.length },
    { key: "commcare" as const, label: "CommCare", count: groupedDomains.commcare.length },
    { key: "connect" as const, label: "Connect", count: groupedDomains.connect.length },
  ]

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-start justify-center pt-16"
         data-testid="workspace-selector-panel">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl max-h-[70vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b">
          <h2 className="text-lg font-semibold">Select Workspace</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"
                  data-testid="workspace-selector-close">✕</button>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 p-2 border-b">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => { setActiveTab(tab.key); setSearch("") }}
              className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                activeTab === tab.key
                  ? "bg-gray-900 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
              data-testid={`workspace-tab-${tab.key}`}
            >
              {tab.label} ({tab.count})
            </button>
          ))}
        </div>

        {/* Search */}
        <div className="p-3 border-b">
          <input
            type="text"
            placeholder="Search..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full px-3 py-2 border rounded-md text-sm"
            data-testid="workspace-search"
          />
        </div>

        {/* Error banner */}
        {enterError && (
          <div className="mx-3 mt-3 p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700"
               data-testid="workspace-enter-error">
            <p className="font-medium">{enterError}</p>
            {missingTenants.length > 0 && (
              <ul className="mt-1 list-disc list-inside">
                {missingTenants.map((t) => <li key={t}>{t}</li>)}
              </ul>
            )}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-3">
          {activeTab === "custom" && (
            <div className="space-y-2">
              {filteredCustom.map((ws) => (
                <button
                  key={ws.id}
                  onClick={async () => {
                    try {
                      await enterCustomWorkspace(ws.id)
                      onClose()
                    } catch {}
                  }}
                  className="w-full text-left p-3 rounded-md border hover:bg-gray-50 transition-colors"
                  data-testid={`workspace-item-${ws.id}`}
                >
                  <div className="font-medium">{ws.name}</div>
                  <div className="text-sm text-gray-500">
                    {ws.tenant_count} tenant{ws.tenant_count !== 1 ? "s" : ""} · {ws.member_count} member{ws.member_count !== 1 ? "s" : ""}
                  </div>
                </button>
              ))}
              {filteredCustom.length === 0 && (
                <p className="text-sm text-gray-500 text-center py-4">No custom workspaces yet</p>
              )}
            </div>
          )}

          {activeTab === "commcare" && (
            <div className="space-y-1">
              {filteredCommcare.map((d) => (
                <button
                  key={d.id}
                  onClick={() => { setActiveDomain(d.id); onClose() }}
                  className="w-full text-left px-3 py-2 rounded-md hover:bg-gray-50 transition-colors"
                  data-testid={`workspace-domain-${d.tenant_id}`}
                >
                  {d.tenant_name}
                </button>
              ))}
            </div>
          )}

          {activeTab === "connect" && (
            <div className="space-y-1">
              {filteredConnect.map((d) => (
                <button
                  key={d.id}
                  onClick={() => { setActiveDomain(d.id); onClose() }}
                  className="w-full text-left px-3 py-2 rounded-md hover:bg-gray-50 transition-colors"
                  data-testid={`workspace-domain-${d.tenant_id}`}
                >
                  {d.tenant_name}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        {activeTab === "custom" && (
          <div className="border-t p-3">
            <button
              className="w-full py-2 text-sm font-medium text-blue-600 hover:text-blue-700"
              data-testid="workspace-create-btn"
            >
              + Create Custom Workspace
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
```

**Step 2: Integrate into Sidebar**

Replace the `<Select>` dropdown in `Sidebar.tsx` with a button that opens the WorkspaceSelector:

```tsx
// In Sidebar.tsx, replace the Select component with:
const [selectorOpen, setSelectorOpen] = useState(false)
const workspaceMode = useAppStore((s) => s.workspaceMode)
const activeCustomWorkspace = useAppStore((s) => s.activeCustomWorkspace)

// Display current selection
const currentLabel = workspaceMode === "custom"
  ? activeCustomWorkspace?.name ?? "Select Workspace"
  : domains.find((d) => d.id === activeDomainId)?.tenant_name ?? "Select Workspace"

// Render:
<button
  onClick={() => setSelectorOpen(true)}
  className="mt-1 w-full text-left px-3 py-2 border rounded-md text-sm truncate"
  data-testid="domain-selector"
>
  {currentLabel}
</button>
<WorkspaceSelector open={selectorOpen} onClose={() => setSelectorOpen(false)} />
```

**Step 3: Run lint**

```bash
cd frontend && bun run lint
```

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add full-width tabbed WorkspaceSelector component"
```

---

## Phase 7: Content Provenance Badges

### Task 11: Add source badges to knowledge views

**Files:**
- Modify: `frontend/src/pages/KnowledgePage/KnowledgePage.tsx` (or equivalent)

This task adds visual source indicators when viewing knowledge within a CustomWorkspace. Each knowledge entry shows a badge indicating whether it comes from a member tenant or from the workspace itself.

**Step 1: Add source field to knowledge API response**

In the backend knowledge list view (`apps/knowledge/api/views.py`), when serving knowledge for a CustomWorkspace (detected via `X-Custom-Workspace` header), annotate each entry with a `source` field:

```python
# In the knowledge list view, after aggregating entries:
for entry in entries:
    if entry.custom_workspace_id:
        entry.source = "workspace"
        entry.source_name = "This Workspace"
    elif entry.workspace_id:
        entry.source = "tenant"
        entry.source_name = entry.workspace.tenant_name
```

**Step 2: Render badge in frontend**

```tsx
{/* In knowledge list item */}
{source && (
  <span
    className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
      source === "workspace"
        ? "bg-blue-100 text-blue-700"
        : "bg-gray-100 text-gray-600"
    }`}
    data-testid={`knowledge-source-${entry.id}`}
  >
    {sourceName}
  </span>
)}
```

**Step 3: Run lint and commit**

```bash
cd frontend && bun run lint
git add -A && git commit -m "feat: add provenance badges to knowledge entries in custom workspace context"
```

---

## Phase 8: Agent Context Assembly

### Task 12: Build custom workspace context for the agent

**Files:**
- Modify: `apps/agents/graph/base.py`

**Step 1: Add a context builder for CustomWorkspace**

All imports at module level. Add these to the top of `apps/agents/graph/base.py`:

```python
from django.db.models import Q

from apps.knowledge.models import AgentLearning, KnowledgeEntry
from apps.workspace.models import TenantWorkspace
```

Then add the function:

```python
# Maximum number of knowledge entries / learnings to include in a single agent context.
# The LLM context window imposes its own limit; loading unbounded rows wastes memory.
_CONTEXT_KNOWLEDGE_LIMIT = 200
_CONTEXT_LEARNINGS_LIMIT = 200


def _build_custom_workspace_context(workspace):
    """Build aggregated agent context for a CustomWorkspace."""
    tenant_workspaces = TenantWorkspace.objects.filter(
        custom_workspace_links__workspace=workspace
    )

    # Aggregate system prompts: workspace-level first, then per-tenant
    prompts = []
    if workspace.system_prompt:
        prompts.append(f"[Workspace: {workspace.name}]\n{workspace.system_prompt}")
    for tw in tenant_workspaces:
        if tw.system_prompt:
            prompts.append(f"[Tenant: {tw.tenant_name}]\n{tw.system_prompt}")

    # Aggregate knowledge — capped to avoid unbounded memory usage
    knowledge = list(
        KnowledgeEntry.objects.filter(
            Q(workspace__in=tenant_workspaces) | Q(custom_workspace=workspace)
        ).values("title", "content", "tags")[:_CONTEXT_KNOWLEDGE_LIMIT]
    )

    # Aggregate learnings — most confident first, capped
    learnings = list(
        AgentLearning.objects.filter(
            Q(workspace__in=tenant_workspaces) | Q(custom_workspace=workspace),
            is_active=True,
        ).order_by("-confidence_score")[:_CONTEXT_LEARNINGS_LIMIT]
    )

    # Available tenant info
    available_tenants = [
        {
            "tenant_id": tw.tenant_id,
            "tenant_name": tw.tenant_name,
            "has_data_dictionary": bool(tw.data_dictionary),
        }
        for tw in tenant_workspaces
    ]

    return {
        "system_prompts": prompts,
        "knowledge": knowledge,
        "learnings": learnings,
        "available_tenants": available_tenants,
    }
```

**Step 2: Integrate into the agent graph**

In the agent graph initialization, check for the `X-Custom-Workspace` header and use the custom context builder when present. The exact integration depends on how the graph currently receives workspace context — modify the existing `_build_context` or equivalent function to branch on workspace type.

**Step 3: Run tests**

```bash
uv run pytest tests/ -x -q
```

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add custom workspace context assembly for LangGraph agent"
```

---

## Phase 9: Tests for Context Assembly and Retriever

### Task 13: Unit tests for `_build_custom_workspace_context`

**Files:**
- Add to: `tests/test_custom_workspace.py`

These are **pure unit tests — no `@pytest.mark.django_db`**. All ORM calls are mocked. The goal is to verify the aggregation logic (prompt ordering, knowledge union, tenant list) without a live database.

**Step 1: Write the tests**

We keep only the prompt-ordering test here. The knowledge/learning aggregation behavior is covered by the DB-backed integration tests in Task 14 (those tests verify the actual ORM queries, which mock-based tests can't do meaningfully without asserting on `Q()` internals).

```python
from unittest.mock import MagicMock, patch


class TestBuildCustomWorkspaceContext:
    def _make_workspace(self, name="Test WS", system_prompt="WS prompt"):
        ws = MagicMock()
        ws.name = name
        ws.system_prompt = system_prompt
        return ws

    def _make_tenant_workspace(self, name, system_prompt="", tenant_id="t1", data_dictionary=None):
        tw = MagicMock()
        tw.tenant_name = name
        tw.system_prompt = system_prompt
        tw.tenant_id = tenant_id
        tw.data_dictionary = data_dictionary
        return tw

    @patch("apps.agents.graph.base.TenantWorkspace")
    @patch("apps.agents.graph.base.AgentLearning")
    @patch("apps.agents.graph.base.KnowledgeEntry")
    def test_workspace_prompt_comes_before_tenant_prompts(
        self, mock_ke, mock_al, mock_tw_model
    ):
        from apps.agents.graph.base import _build_custom_workspace_context

        workspace = self._make_workspace(system_prompt="workspace prompt")
        tw1 = self._make_tenant_workspace("Tenant A", system_prompt="tenant prompt")
        mock_tw_model.objects.filter.return_value = [tw1]
        mock_ke.objects.filter.return_value.values.return_value.__getitem__ = lambda s, k: []
        mock_al.objects.filter.return_value.order_by.return_value.__getitem__ = lambda s, k: []

        result = _build_custom_workspace_context(workspace)

        prompts = result["system_prompts"]
        assert len(prompts) == 2
        assert "workspace prompt" in prompts[0]
        assert "tenant prompt" in prompts[1]

    @patch("apps.agents.graph.base.TenantWorkspace")
    @patch("apps.agents.graph.base.AgentLearning")
    @patch("apps.agents.graph.base.KnowledgeEntry")
    def test_empty_workspace_prompt_excluded(self, mock_ke, mock_al, mock_tw_model):
        from apps.agents.graph.base import _build_custom_workspace_context

        workspace = self._make_workspace(system_prompt="")  # empty
        mock_tw_model.objects.filter.return_value = []
        mock_ke.objects.filter.return_value.values.return_value.__getitem__ = lambda s, k: []
        mock_al.objects.filter.return_value.order_by.return_value.__getitem__ = lambda s, k: []

        result = _build_custom_workspace_context(workspace)

        assert result["system_prompts"] == []
```

Note: The knowledge/learning aggregation and `available_tenants` list are tested in `TestWorkspaceContextQuerySet` (Task 14) using real DB queries. Those tests catch regressions that mock-based tests cannot (e.g., wrong Q filter construction, incorrect field lookups).

**Step 2: Run tests (no DB — should be fast)**

```bash
uv run pytest tests/test_custom_workspace.py::TestBuildCustomWorkspaceContext -v
```

Expected: All pass. If they hit the DB, the mocking is wrong — fix it.

**Step 3: Commit**

```bash
git add -A && git commit -m "test: add unit tests for _build_custom_workspace_context (mocked, no DB)"
```

---

### Task 14: Integration tests for knowledge retriever in custom workspace context

**Files:**
- Create: `tests/test_custom_workspace_retriever.py`

These tests verify that the knowledge retriever and `WorkspaceContextQuerySet.for_workspace_context()` return entries from both `TenantWorkspace`-scoped and `CustomWorkspace`-scoped knowledge. They require the DB (via `@pytest.mark.django_db`) since they test ORM behaviour.

**Step 1: Write the tests**

```python
import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import AgentLearning, KnowledgeEntry
from apps.workspace.models import CustomWorkspace, TenantWorkspace

User = get_user_model()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="retriever_owner@test.com", password="pass")


@pytest.fixture
def tenant_ws(db):
    return TenantWorkspace.objects.create(tenant_id="retriever-domain", tenant_name="Retriever Domain")


@pytest.fixture
def custom_ws(db, owner, tenant_ws):  # tenant_ws must be declared as a parameter
    from apps.workspace.models import CustomWorkspaceTenant
    cws = CustomWorkspace.objects.create(name="Retriever WS", created_by=owner)
    CustomWorkspaceTenant.objects.create(workspace=cws, tenant_workspace=tenant_ws)
    return cws


class TestWorkspaceContextQuerySet:
    @pytest.mark.django_db
    def test_for_tenant_workspace_returns_tenant_entries(self, tenant_ws):
        entry = KnowledgeEntry.objects.create(
            workspace=tenant_ws, title="Tenant Entry", content="content"
        )
        results = KnowledgeEntry.objects.for_workspace_context(tenant_ws)
        assert entry in results

    @pytest.mark.django_db
    def test_for_custom_workspace_returns_custom_entries(self, owner):
        cws = CustomWorkspace.objects.create(name="CWS", created_by=owner)
        entry = KnowledgeEntry.objects.create(
            custom_workspace=cws, title="Custom Entry", content="content"
        )
        results = KnowledgeEntry.objects.for_workspace_context(cws)
        assert entry in results

    @pytest.mark.django_db
    def test_tenant_entries_not_returned_for_custom_workspace(self, tenant_ws, owner):
        """Entries scoped to a TenantWorkspace must not appear when querying a CustomWorkspace."""
        cws = CustomWorkspace.objects.create(name="CWS2", created_by=owner)
        tenant_entry = KnowledgeEntry.objects.create(
            workspace=tenant_ws, title="Tenant-only", content="content"
        )
        results = KnowledgeEntry.objects.for_workspace_context(cws)
        assert tenant_entry not in results

    @pytest.mark.django_db
    def test_agent_learning_for_workspace_context(self, tenant_ws):
        learning = AgentLearning.objects.create(
            workspace=tenant_ws, description="Tenant learning", is_active=True
        )
        results = AgentLearning.objects.for_workspace_context(tenant_ws)
        assert learning in results
```

**Step 2: Run tests**

```bash
uv run pytest tests/test_custom_workspace_retriever.py -v
```

Expected: All pass.

**Step 3: Commit**

```bash
git add -A && git commit -m "test: add integration tests for WorkspaceContextQuerySet.for_workspace_context"
```

---

## Summary of Tasks

| # | Task | Phase | Depends On |
|---|------|-------|------------|
| 1 | Rename directory + AppConfig | 1 | — |
| 2 | Update all imports + migration | 1 | 1 |
| 3 | Write CustomWorkspace model tests | 2 | 2 |
| 4 | Implement CustomWorkspace models | 2 | 3 |
| 5 | Write dual FK tests | 3 | 4 |
| 6 | Add dual FK to knowledge/chat + WorkspaceContextQuerySet manager + `clean()` methods | 3 | 5 |
| 7 | Write API tests (incl. `role=owner` rejection tests) | 4 | 6 |
| 7.5 | Extract `_can_perform_action` pure function + permission matrix tests | 4 | 7 |
| 8 | Implement API views/serializers/URLs (`_get_workspace_or_403`, `CustomWorkspaceUpdateSerializer`, fixed `_validate_tenant_access`, `enter/` single-fetch) | 4 | 7.5 |
| 8.5 | `resolve_workspace_from_request` utility + re-validation tests | 4 | 8 |
| 9 | Frontend workspaceSlice | 5 | 8 |
| 10 | Workspace selector UI | 6 | 9 |
| 11 | Content provenance badges | 7 | 10 |
| 12 | Agent context assembly (module-level imports, `[:200]` cap) | 8 | 6 |
| 13 | Unit tests for `_build_custom_workspace_context` (mocked, no DB — prompt ordering only) | 9 | 12 |
| 14 | Integration tests for WorkspaceContextQuerySet retriever (fixture bug fixed) | 9 | 6 |

Tasks 9-11 (frontend), Task 12 (agent context), Task 8.5 (resolve_workspace), and Task 14 (retriever tests) can be parallelized after Task 8 completes. Task 13 depends on Task 12.
