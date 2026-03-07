# Spec Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bring Scout's codebase into alignment with the core design specification, building a workspace/membership layer on top of the existing tenant foundation, then adding RBAC, invitations, audit log, and schema lifecycle automation.

**Architecture:** The existing Tenant/TenantMembership/TenantSchema infrastructure is kept as the data layer. A new workspace layer (Workspace, WorkspaceMembership) sits on top as the user-facing abstraction. All content models (Thread, Artifact, Recipe, knowledge) are scoped to Workspace, not TenantMembership. Roles (Read, ReadWrite, Manage) are enforced via DRF permission classes backed by WorkspaceMembership.

**Tech Stack:** Django 5, DRF, PostgreSQL, Celery + django-celery-beat, allauth (OAuth), uv, pytest, ruff

---

## Design Decisions

Read this before touching any code.

### The Workspace model

`TenantWorkspace` becomes `Workspace`. It is no longer 1:1 with Tenant — it links to one or more tenants via a `WorkspaceTenant` junction table. For Phase 1 (single-tenant workspaces), you will not implement multi-tenant logic, but the model must support it from the start.

```python
# apps/projects/models.py

class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    name = models.CharField(max_length=255)
    tenants = models.ManyToManyField("users.Tenant", through="WorkspaceTenant", related_name="workspaces")
    is_auto_created = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class WorkspaceTenant(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    tenant = models.ForeignKey("users.Tenant", on_delete=models.CASCADE)
    class Meta:
        unique_together = [["workspace", "tenant"]]
```

### Auto-created workspaces

When `TenantMembership` is created (in the OAuth signal), the system must immediately:
1. Create a `Workspace` (name = tenant.canonical_name, is_auto_created=True, created_by=user)
2. Create a `WorkspaceTenant` linking workspace → tenant
3. Create a `WorkspaceMembership` for the user with role=MANAGE

The lazy `get_or_create` in `workspace_resolver.py` is removed. All workspaces exist before the user first touches the UI.

### WorkspaceMembership roles

```python
class WorkspaceRole(models.TextChoices):
    READ = "read", "Read"
    READ_WRITE = "read_write", "Read/Write"
    MANAGE = "manage", "Manage"

class WorkspaceMembership(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspace_memberships")
    role = models.CharField(max_length=20, choices=WorkspaceRole.choices)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        unique_together = [["workspace", "user"]]
```

### TenantSchema ownership

`TenantSchema` currently FKs to `TenantMembership`. The spec says one schema per Tenant, shared across all users. Fix this in Phase 1: change the FK to `Tenant`. Since SchemaManager already deduplicates by `schema_name`, this is safe.

### Thread workspace FK

`Thread.tenant_membership` FK is replaced by `Thread.workspace` FK. This is done in Phase 2 (after Workspace exists). Until then, threads continue using `tenant_membership`.

### Soft delete pattern

Both `Artifact` and `Recipe` get `is_deleted = BooleanField(default=False)`. Use a custom default manager that filters `is_deleted=False`, and an `all_objects` manager that does not. Views use the default manager; superuser/admin uses `all_objects`.

### Permission classes

All workspace-scoped views get a `WorkspacePermission` mixin that:
1. Resolves workspace from `workspace_id` URL parameter (UUID of `Workspace`)
2. Checks `WorkspaceMembership` for the requesting user
3. Returns 403 if not a member or if role is insufficient

The old `resolve_workspace(request, tenant_id)` helper (using TenantMembership.id) is replaced by `resolve_workspace(request, workspace_id)` (using Workspace.id). Update all call sites.

### "Deleted user" display

A helper `user_display_name(user)` returns `user.get_full_name()` if the user exists, else `"Deleted user"`. Used in all serializers that expose creator attribution.

---

## Phase 1: Correctness Fixes

Fix existing code before adding anything new. These changes are independent and can be done in any order within the phase.

---

### Task 1.1: Fix TenantSchema FK to point to Tenant

**Why:** The spec says one schema per Tenant, shared across all users. Currently it FKs to TenantMembership, which is wrong conceptually even though schema_name deduplication masks the problem.

**Files:**
- Modify: `apps/projects/models.py`
- Modify: `apps/projects/services/schema_manager.py`
- Modify: `mcp_server/context.py` (if it references tenant_membership via schema)
- Create: `apps/projects/migrations/XXXX_tenantschema_fk_to_tenant.py`

**Step 1: Write a failing test**

```python
# tests/test_projects_models.py
def test_tenant_schema_belongs_to_tenant_not_membership(tenant, user):
    schema = TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_schema",
        state=SchemaState.ACTIVE,
    )
    assert schema.tenant == tenant
    # old attribute must not exist
    assert not hasattr(schema, 'tenant_membership_id')
```

Run: `uv run pytest tests/test_projects_models.py::test_tenant_schema_belongs_to_tenant_not_membership -v`
Expected: FAIL — `TenantSchema` has no `tenant` field

**Step 2: Update the model**

In `apps/projects/models.py`, change `TenantSchema.tenant_membership` FK to `tenant`:

```python
class TenantSchema(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="schemas",
    )
    schema_name = models.CharField(max_length=255, unique=True)
    state = models.CharField(max_length=20, choices=SchemaState.choices, default=SchemaState.PROVISIONING)
    created_at = models.DateTimeField(auto_now_add=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)  # explicit, not auto_now

    def touch(self):
        """Call this on user-initiated actions to reset the inactivity TTL."""
        from django.utils import timezone
        self.last_accessed_at = timezone.now()
        self.save(update_fields=["last_accessed_at"])
```

**Step 3: Generate migration**

```bash
uv run python manage.py makemigrations projects --name tenantschema_fk_to_tenant
```

**Step 4: Update schema_manager.py**

Update `SchemaManager.provision()` and `SchemaManager.teardown()` to accept `tenant` instead of `tenant_membership`. Search for all callers and update them.

**Step 5: Run tests**

```bash
uv run pytest tests/test_projects_models.py -v
```

**Step 6: Commit**

```bash
git add apps/projects/models.py apps/projects/migrations/ apps/projects/services/schema_manager.py
git commit -m "refactor: TenantSchema FK tenant_membership → tenant"
```

---

### Task 1.2: Fix last_accessed_at (remove auto_now)

`auto_now=True` was already removed in Task 1.1 above (the field is now `null=True, blank=True` with an explicit `touch()` method). Ensure all places that should touch last_accessed_at do so via `schema.touch()`:

- After a chat message is sent: `apps/chat/views.py` (post-message)
- After an artifact query: `apps/artifacts/views.py` (ArtifactQueryDataView)
- After a recipe run: `apps/recipes/views.py` (RecipeRunView)
- After MCP `query` tool is called: `mcp_server/server.py`

**Rule:** Do NOT call `schema.touch()` in background tasks, materialisation, or schema health checks. Only user-initiated actions reset the TTL.

**Step 1: Write a test**

```python
# tests/test_schema_ttl.py
from django.utils import timezone
import freezegun

def test_touch_updates_last_accessed_at(tenant_schema):
    original = tenant_schema.last_accessed_at
    with freezegun.freeze_time("2026-01-01 12:00:00"):
        tenant_schema.touch()
    tenant_schema.refresh_from_db()
    assert tenant_schema.last_accessed_at == timezone.datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert tenant_schema.last_accessed_at != original

def test_saving_schema_without_touch_does_not_update_last_accessed_at(tenant_schema):
    original = tenant_schema.last_accessed_at
    tenant_schema.schema_name = tenant_schema.schema_name  # no-op save
    tenant_schema.save(update_fields=["schema_name"])
    tenant_schema.refresh_from_db()
    assert tenant_schema.last_accessed_at == original
```

Run: `uv run pytest tests/test_schema_ttl.py -v`

**Step 2: Commit**

```bash
git commit -m "fix: TenantSchema last_accessed_at must be touched explicitly"
```

---

### Task 1.3: Fix Artifact.created_by on_delete

`created_by` uses `CASCADE` which deletes artifacts when the creator is deleted. Should be `SET_NULL` so artifacts persist with `created_by=None`.

**Files:**
- Modify: `apps/artifacts/models.py`
- Create: migration

**Step 1: Write test**

```python
# tests/test_artifact_attribution.py
def test_artifact_survives_user_deletion(workspace, artifact):
    artifact.created_by.delete()
    artifact.refresh_from_db()
    assert artifact.created_by is None
```

**Step 2: Fix the model**

In `apps/artifacts/models.py`, change `created_by`:

```python
created_by = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name="created_artifacts",
)
```

**Step 3: Migrate and test**

```bash
uv run python manage.py makemigrations artifacts --name artifact_created_by_set_null
uv run pytest tests/test_artifact_attribution.py -v
git commit -m "fix: Artifact.created_by on_delete SET_NULL (was CASCADE)"
```

---

### Task 1.4: Add "Deleted user" display helper

Create a shared utility and apply it to artifact and recipe serializers.

**Files:**
- Create: `apps/common/utils.py`
- Modify: `apps/artifacts/serializers.py` (or wherever artifact serialization lives)
- Modify: `apps/recipes/serializers.py`
- Modify: `apps/knowledge/serializers.py`

**Step 1: Write test**

```python
# tests/test_common_utils.py
from apps.common.utils import creator_display_name

def test_creator_display_name_with_user(user):
    assert creator_display_name(user) == user.get_full_name()

def test_creator_display_name_with_none():
    assert creator_display_name(None) == "Deleted user"
```

**Step 2: Create utility**

```python
# apps/common/utils.py
def creator_display_name(user) -> str:
    """Return display name for a content creator, handling deleted accounts."""
    if user is None:
        return "Deleted user"
    return user.get_full_name()
```

**Step 3: Add `created_by_name` field to artifact/recipe serializers**

```python
# In each serializer:
created_by_name = serializers.SerializerMethodField()

def get_created_by_name(self, obj):
    from apps.common.utils import creator_display_name
    return creator_display_name(obj.created_by)
```

**Step 4: Commit**

```bash
git commit -m "feat: add Deleted user attribution for artifacts and recipes"
```

---

### Task 1.5: Add soft delete to Artifact

**Files:**
- Modify: `apps/artifacts/models.py`
- Modify: `apps/artifacts/views.py`
- Create: migration

**Step 1: Write tests**

```python
# tests/test_artifact_soft_delete.py
def test_soft_delete_sets_is_deleted(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    artifact.refresh_from_db()
    assert artifact.is_deleted is True
    assert artifact.deleted_at is not None
    assert artifact.deleted_by == artifact.created_by

def test_soft_deleted_artifact_hidden_from_default_queryset(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    assert Artifact.objects.filter(id=artifact.id).count() == 0

def test_soft_deleted_artifact_visible_via_all_objects(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    assert Artifact.all_objects.filter(id=artifact.id).count() == 1

def test_undelete_restores_artifact(artifact):
    artifact.soft_delete(deleted_by=artifact.created_by)
    artifact.undelete()
    artifact.refresh_from_db()
    assert artifact.is_deleted is False
```

**Step 2: Add soft delete fields and manager**

```python
# apps/artifacts/models.py

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class Artifact(models.Model):
    # ... existing fields ...
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def soft_delete(self, deleted_by):
        from django.utils import timezone
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def undelete(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])
```

**Step 3: Update delete view**

In `apps/artifacts/views.py`, replace `artifact.delete()` with `artifact.soft_delete(deleted_by=request.user)`. Return 204.

**Step 4: Add undelete endpoint**

```
POST /api/artifacts/<id>/undelete/
```

Only accessible to write/manage role (enforced in Phase 2 — for now add the view, leave permission check as IsAuthenticated).

**Step 5: Migrate and test**

```bash
uv run python manage.py makemigrations artifacts --name artifact_soft_delete
uv run pytest tests/test_artifact_soft_delete.py -v
git commit -m "feat: soft delete for artifacts"
```

---

### Task 1.6: Add soft delete to Recipe

Same pattern as Task 1.5. Apply to `Recipe` model and `RecipeDetailView.delete()`.

```bash
uv run python manage.py makemigrations recipes --name recipe_soft_delete
uv run pytest tests/test_recipe_soft_delete.py -v
git commit -m "feat: soft delete for recipes"
```

---

### Task 1.7: Remove PUBLIC artifact sharing

The spec prohibits public sharing of artifacts due to sensitive data risk.

**Files:**
- Modify: `apps/artifacts/models.py` — remove `PUBLIC` from `AccessLevel`
- Modify: `apps/artifacts/views.py` — reject `PUBLIC` at API level
- Create: migration to update any existing PUBLIC records to TENANT

**Step 1: Write test**

```python
def test_cannot_create_public_share(api_client, artifact):
    resp = api_client.post(f"/api/artifacts/{artifact.id}/shares/", {"access_level": "public"})
    assert resp.status_code == 400
```

**Step 2: Block in view**

```python
# In CreateShareView.post():
if request.data.get("access_level") == SharedArtifact.AccessLevel.PUBLIC:
    return Response({"error": "Public sharing is not permitted."}, status=400)
```

**Step 3: Remove the choice from the model** (after migration updates existing rows)

```bash
git commit -m "fix: block public artifact sharing (sensitive data risk)"
```

---

### Task 1.8: Block AgentLearning manual creation via API

**Files:**
- Modify: `apps/knowledge/views.py`

**Step 1: Write test**

```python
def test_cannot_manually_create_agent_learning(api_client, workspace):
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/knowledge/",
        {"type": "learning", "description": "test"},
    )
    assert resp.status_code == 400
    assert "AgentLearning" in resp.data["error"]
```

**Step 2: Add guard in POST handler**

```python
# In KnowledgeListCreateView.post():
if request.data.get("type") == "learning":
    return Response(
        {"error": "AgentLearning entries are created automatically by the agent and cannot be manually created."},
        status=status.HTTP_400_BAD_REQUEST,
    )
```

**Step 3: Test and commit**

```bash
uv run pytest tests/test_knowledge_views.py::test_cannot_manually_create_agent_learning -v
git commit -m "fix: block manual AgentLearning creation via API"
```

---

## Phase 2: Workspace Foundation

This phase introduces the workspace layer that all future features depend on. Do NOT begin Phase 3 until Phase 2 is complete and all tests pass.

---

### Task 2.1: Create Workspace and WorkspaceMembership models

Replace `TenantWorkspace` with `Workspace` (multi-tenant capable from the start). This is the most critical change in the entire plan.

**Files:**
- Modify: `apps/projects/models.py` — replace `TenantWorkspace` with `Workspace`, add `WorkspaceTenant`, `WorkspaceMembership`
- Update all FK references to `TenantWorkspace` in: `apps/artifacts/models.py`, `apps/recipes/models.py`, `apps/knowledge/models.py`
- Create migrations

**New models:**

```python
# apps/projects/models.py

import uuid
from django.conf import settings
from django.db import models


class WorkspaceRole(models.TextChoices):
    READ = "read", "Read"
    READ_WRITE = "read_write", "Read/Write"
    MANAGE = "manage", "Manage"


class Workspace(models.Model):
    """User-facing workspace, layered on top of one or more tenants."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    tenants = models.ManyToManyField(
        "users.Tenant",
        through="WorkspaceTenant",
        related_name="workspaces",
    )
    is_auto_created = models.BooleanField(
        default=False,
        help_text="True if this workspace was automatically created during OAuth login.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    system_prompt = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkspaceTenant(models.Model):
    """Junction table linking a Workspace to a Tenant."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="workspace_tenants")
    tenant = models.ForeignKey("users.Tenant", on_delete=models.CASCADE, related_name="workspace_tenants")

    class Meta:
        unique_together = [["workspace", "tenant"]]


class WorkspaceMembership(models.Model):
    """A user's membership of a workspace with an assigned role."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=20, choices=WorkspaceRole.choices)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["workspace", "user"]]
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.user.email} in {self.workspace.name} ({self.role})"
```

**Step 1: Write tests**

```python
# tests/test_workspace_models.py

def test_workspace_has_name_and_tenants(tenant, user):
    ws = Workspace.objects.create(name="My workspace", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    assert ws.tenants.first() == tenant

def test_workspace_membership_enforces_unique_user_per_workspace(workspace, user):
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.MANAGE)
    with pytest.raises(IntegrityError):
        WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.READ)

def test_workspace_membership_roles():
    assert WorkspaceRole.MANAGE > WorkspaceRole.READ_WRITE  # ordering for permission checks
```

**Step 2: Update content model FKs**

In `apps/artifacts/models.py`, `apps/recipes/models.py`, `apps/knowledge/models.py`:
- Change `workspace = models.ForeignKey("projects.TenantWorkspace", ...)` to `workspace = models.ForeignKey("projects.Workspace", ...)`
- Remove `null=True, blank=True` — workspace should be required
- Add `on_delete=models.CASCADE` (deleting workspace cascades to content)

**Step 3: Generate and run migrations**

```bash
uv run python manage.py makemigrations projects --name workspace_and_membership
uv run python manage.py makemigrations artifacts recipes knowledge --name workspace_fk_to_workspace
uv run pytest -v
git commit -m "feat: Workspace, WorkspaceTenant, WorkspaceMembership models"
```

---

### Task 2.2: Auto-create Workspace + WorkspaceMembership in signal

When `TenantMembership` is created, immediately create a Workspace and WorkspaceMembership for the user.

**Files:**
- Modify: `apps/users/signals.py`
- Modify: `apps/users/services/tenant_resolution.py` (where TenantMembership is created)

**Step 1: Write test**

```python
# tests/test_workspace_auto_creation.py

def test_workspace_auto_created_on_tenant_membership_creation(user, tenant):
    membership = TenantMembership.objects.create(user=user, tenant=tenant)
    # Workspace should be auto-created
    ws = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=user,
        workspace_tenants__tenant=tenant,
    ).first()
    assert ws is not None
    assert ws.name == tenant.canonical_name

def test_auto_created_workspace_gives_user_manage_role(user, tenant):
    TenantMembership.objects.create(user=user, tenant=tenant)
    membership = WorkspaceMembership.objects.get(
        workspace__is_auto_created=True,
        workspace__workspace_tenants__tenant=tenant,
        user=user,
    )
    assert membership.role == WorkspaceRole.MANAGE

def test_auto_creation_is_idempotent(user, tenant):
    """Creating membership twice should not create duplicate workspaces."""
    TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    count = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=user,
        workspace_tenants__tenant=tenant,
    ).count()
    assert count == 1
```

**Step 2: Add post_save signal on TenantMembership**

```python
# apps/users/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="users.TenantMembership")
def auto_create_workspace_on_membership(sender, instance, created, **kwargs):
    """Auto-create a workspace for newly created TenantMembership records."""
    if not created:
        return
    from apps.projects.models import Workspace, WorkspaceTenant, WorkspaceMembership, WorkspaceRole
    # Check if an auto-created workspace for this user+tenant already exists
    existing = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=instance.user,
        workspace_tenants__tenant=instance.tenant,
    ).first()
    if existing:
        return
    workspace = Workspace.objects.create(
        name=instance.tenant.canonical_name,
        is_auto_created=True,
        created_by=instance.user,
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=instance.tenant)
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=instance.user,
        role=WorkspaceRole.MANAGE,
    )
```

**Step 3: Connect signal in AppConfig**

```python
# apps/users/apps.py
class UsersConfig(AppConfig):
    def ready(self):
        import apps.users.signals  # noqa: F401
```

**Step 4: Remove workspace_resolver.py lazy creation**

Update `apps/projects/workspace_resolver.py` to look up workspace by UUID rather than creating it. The new signature is:

```python
def resolve_workspace(request, workspace_id):
    """Resolve Workspace from workspace_id URL parameter.

    Checks that the requesting user is a member. Returns (workspace, membership, None)
    on success or (None, None, Response(403)) on error.
    """
    try:
        membership = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id,
            user=request.user,
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None, Response({"error": "Workspace not found or access denied."}, status=403)
    return membership.workspace, membership, None
```

Update all call sites to pass `workspace_id` (the URL parameter) and accept the membership as third return value.

**Step 5: Test and commit**

```bash
uv run pytest tests/test_workspace_auto_creation.py -v
git commit -m "feat: auto-create workspace + membership on TenantMembership creation"
```

---

### Task 2.3: DRF permission classes for workspace roles

**Files:**
- Create: `apps/projects/permissions.py`

**Step 1: Write tests**

```python
# tests/test_workspace_permissions.py

def test_read_permission_allows_read_member(api_client, workspace, read_user):
    api_client.force_login(read_user)
    resp = api_client.get(f"/api/workspaces/{workspace.id}/artifacts/")
    assert resp.status_code == 200

def test_read_permission_denies_non_member(api_client, workspace, other_user):
    api_client.force_login(other_user)
    resp = api_client.get(f"/api/workspaces/{workspace.id}/artifacts/")
    assert resp.status_code == 403

def test_write_permission_denies_read_only_member(api_client, workspace, read_user):
    api_client.force_login(read_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/artifacts/", {...})
    assert resp.status_code == 403

def test_manage_permission_denies_read_write_member(api_client, workspace, write_user):
    api_client.force_login(write_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/members/", {...})
    assert resp.status_code == 403
```

**Step 2: Create permission classes**

```python
# apps/projects/permissions.py

from rest_framework.permissions import BasePermission
from apps.projects.models import WorkspaceMembership, WorkspaceRole


def _get_membership(request, view):
    workspace_id = view.kwargs.get("workspace_id")
    if not workspace_id:
        return None
    try:
        return WorkspaceMembership.objects.get(
            workspace_id=workspace_id,
            user=request.user,
        )
    except WorkspaceMembership.DoesNotExist:
        return None


class IsWorkspaceMember(BasePermission):
    """Allows any workspace member (any role)."""

    def has_permission(self, request, view):
        return _get_membership(request, view) is not None


class IsWorkspaceReadWrite(BasePermission):
    """Allows read_write and manage role members."""

    def has_permission(self, request, view):
        m = _get_membership(request, view)
        return m is not None and m.role in (WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE)


class IsWorkspaceManager(BasePermission):
    """Allows manage role members only."""

    def has_permission(self, request, view):
        m = _get_membership(request, view)
        return m is not None and m.role == WorkspaceRole.MANAGE
```

**Step 3: Apply to content views (read-gated)**

Update URL patterns to nest content under `/api/workspaces/<workspace_id>/`. Apply:
- `IsWorkspaceMember` — GET list/detail for artifacts, recipes, knowledge, threads
- `IsWorkspaceReadWrite` — POST/PUT/PATCH/DELETE for artifacts, recipes, knowledge; POST for threads; running recipes
- `IsWorkspaceManager` — workspace settings, member management (Phase 3)

Pattern for mixed permissions:

```python
# In a view's get_permissions():
def get_permissions(self):
    if self.request.method in ("GET", "HEAD", "OPTIONS"):
        return [IsAuthenticated(), IsWorkspaceMember()]
    return [IsAuthenticated(), IsWorkspaceReadWrite()]
```

**Step 4: Test and commit**

```bash
uv run pytest tests/test_workspace_permissions.py -v
git commit -m "feat: workspace role permission classes"
```

---

### Task 2.4: Re-nest all content API URLs under /api/workspaces/<workspace_id>/

**Files:**
- Modify: `config/urls.py`
- Modify: `apps/artifacts/urls.py`
- Modify: `apps/recipes/urls.py`
- Modify: `apps/knowledge/urls.py`
- Modify: `apps/chat/urls.py`
- Modify: all views in those apps (remove internal `resolve_workspace` calls; use `workspace_id` from kwargs)

**New URL structure:**

```
/api/workspaces/                              GET  — list user's workspaces
/api/workspaces/<workspace_id>/               GET/PATCH/DELETE — workspace detail
/api/workspaces/<workspace_id>/members/       GET/POST
/api/workspaces/<workspace_id>/members/<id>/  PATCH/DELETE

/api/workspaces/<workspace_id>/artifacts/
/api/workspaces/<workspace_id>/artifacts/<id>/
/api/workspaces/<workspace_id>/artifacts/<id>/undelete/
/api/workspaces/<workspace_id>/artifacts/<id>/data/

/api/workspaces/<workspace_id>/recipes/
/api/workspaces/<workspace_id>/recipes/<id>/
/api/workspaces/<workspace_id>/recipes/<id>/run/

/api/workspaces/<workspace_id>/knowledge/
/api/workspaces/<workspace_id>/knowledge/<id>/

/api/workspaces/<workspace_id>/threads/
/api/workspaces/<workspace_id>/threads/<thread_id>/
/api/workspaces/<workspace_id>/threads/<thread_id>/share/
/api/chat/                                    POST — send message (workspace_id in body)

/api/workspaces/<workspace_id>/data-dictionary/
/api/workspaces/<workspace_id>/data-dictionary/tables/<name>/
/api/workspaces/<workspace_id>/refresh/
```

**Step 1: Write integration tests for new URL structure**

```python
# tests/test_api_urls.py
def test_artifacts_nested_under_workspace(api_client, workspace, write_user):
    api_client.force_login(write_user)
    resp = api_client.get(f"/api/workspaces/{workspace.id}/artifacts/")
    assert resp.status_code == 200
```

**Step 2: Implement and commit**

```bash
git commit -m "refactor: nest all content APIs under /api/workspaces/<workspace_id>/"
```

---

## Phase 3: Workspace Management API

---

### Task 3.1: Workspace list and create endpoints

**Files:**
- Create: `apps/projects/api/workspace_views.py`

```
GET  /api/workspaces/          — list workspaces user is a member of
POST /api/workspaces/          — create a new workspace
```

**Workspace list serializer** should include: `id`, `name`, `is_auto_created`, `role` (user's role), `tenant_count`, `member_count`, `created_at`.

**Create rules:**
- Name required
- At least one `tenant_id` required (UUID of a Tenant the user has access to via TenantMembership)
- Creates WorkspaceTenant junction records
- Creates WorkspaceMembership for creator with role=MANAGE

**Step 1: Tests**

```python
def test_list_workspaces_returns_only_users_workspaces(api_client, user, other_workspace):
    resp = api_client.get("/api/workspaces/")
    ids = [w["id"] for w in resp.data]
    assert str(other_workspace.id) not in ids

def test_create_workspace(api_client, user, tenant_membership):
    resp = api_client.post("/api/workspaces/", {
        "name": "My new workspace",
        "tenant_ids": [str(tenant_membership.tenant.id)],
    })
    assert resp.status_code == 201
    assert resp.data["name"] == "My new workspace"
    # Creator gets manage role
    assert WorkspaceMembership.objects.filter(
        workspace_id=resp.data["id"], user=user, role=WorkspaceRole.MANAGE
    ).exists()

def test_cannot_create_workspace_for_tenant_user_cannot_access(api_client, other_tenant):
    resp = api_client.post("/api/workspaces/", {"name": "Bad", "tenant_ids": [str(other_tenant.id)]})
    assert resp.status_code == 400
```

**Step 2: Implement and commit**

```bash
git commit -m "feat: workspace list and create endpoints"
```

---

### Task 3.2: Workspace rename and delete

```
PATCH  /api/workspaces/<workspace_id>/   — rename (manage role only)
DELETE /api/workspaces/<workspace_id>/   — delete workspace (manage role only)
```

**Delete rules:**
- Cascade: delete all threads, artifacts, recipes, knowledge (via `CASCADE` on FKs — already set up in Task 2.1)
- Invalidate share tokens: set `Thread.share_token=None` for all threads in workspace
- A user cannot delete their last workspace covering any tenant — validate this before deletion

**Step 1: Tests**

```python
def test_rename_workspace_requires_manage_role(api_client, workspace, write_user):
    api_client.force_login(write_user)
    resp = api_client.patch(f"/api/workspaces/{workspace.id}/", {"name": "New name"})
    assert resp.status_code == 403

def test_delete_workspace_cascades_to_threads(api_client, workspace, thread, manage_user):
    thread_id = thread.id
    api_client.force_login(manage_user)
    api_client.delete(f"/api/workspaces/{workspace.id}/")
    assert not Thread.objects.filter(id=thread_id).exists()

def test_cannot_delete_last_workspace_for_tenant(api_client, workspace, manage_user):
    # workspace is the only one covering its tenant for this user
    resp = api_client.delete(f"/api/workspaces/{workspace.id}/")
    assert resp.status_code == 400
    assert "last workspace" in resp.data["error"].lower()
```

**Step 2: Implement and commit**

```bash
git commit -m "feat: workspace rename and delete with cascade"
```

---

### Task 3.3: Workspace member management

```
GET    /api/workspaces/<workspace_id>/members/       — list members (any member)
PATCH  /api/workspaces/<workspace_id>/members/<id>/  — change role (manage only)
DELETE /api/workspaces/<workspace_id>/members/<id>/  — remove member (manage only)
```

**Rules:**
- Cannot demote or remove the last manage-role user in a workspace
- A user can remove themselves from a workspace as long as they're not the last manager
- Removing a member deletes their threads (cascade from workspace deletion is not appropriate here since the workspace still exists; explicitly delete user's threads in the workspace)

**Step 1: Tests**

```python
def test_cannot_remove_last_manager(api_client, workspace, manage_user):
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=manage_user)
    resp = api_client.delete(f"/api/workspaces/{workspace.id}/members/{membership.id}/")
    assert resp.status_code == 400
    assert "last" in resp.data["error"].lower()

def test_removing_member_deletes_their_threads(api_client, workspace, write_user, thread_by_write_user):
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=write_user)
    api_client.delete(f"/api/workspaces/{workspace.id}/members/{membership.id}/")
    assert not Thread.objects.filter(id=thread_by_write_user.id).exists()

def test_removed_members_artifacts_remain(api_client, workspace, write_user, artifact_by_write_user):
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=write_user)
    api_client.delete(f"/api/workspaces/{workspace.id}/members/{membership.id}/")
    assert Artifact.objects.filter(id=artifact_by_write_user.id).exists()
```

**Step 2: Implement and commit**

```bash
git commit -m "feat: workspace member management with last-manager protection"
```

---

### Task 3.4: Migrate Thread to use Workspace FK

`Thread.tenant_membership` → `Thread.workspace`

**Files:**
- Modify: `apps/chat/models.py`
- Modify: `apps/chat/views.py`
- Create: migration

**Step 1: Write test**

```python
def test_thread_belongs_to_workspace(workspace, user):
    thread = Thread.objects.create(workspace=workspace, user=user, title="Test")
    assert thread.workspace == workspace
```

**Step 2: Update model**

```python
class Thread(models.Model):
    workspace = models.ForeignKey(
        "projects.Workspace",
        on_delete=models.CASCADE,
        related_name="threads",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="threads")
    # Remove tenant_membership FK
    title = models.CharField(max_length=200, default="New chat")
    is_shared = models.BooleanField(default=False)
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

Note: `is_public` is removed — sharing is now a simple `is_shared` flag generating a `share_token`. Named non-member grants are handled in a later phase.

**Step 3: Update chat views**

Update `POST /api/chat/` to accept `workspace_id` in the request body instead of `tenant_id`. Update thread list/detail views to filter by `workspace`.

**Step 4: Migrate and commit**

```bash
uv run python manage.py makemigrations chat --name thread_workspace_fk
uv run pytest tests/test_chat/ -v
git commit -m "refactor: Thread.workspace FK (replaces tenant_membership)"
```

---

## Phase 4: Schema Lifecycle

---

### Task 4.1: Implement data refresh endpoint

The stub in `RefreshSchemaView` must be replaced with real logic: provision a new schema in the background, cut over on success, stay on old schema on failure.

**Files:**
- Modify: `apps/projects/api/views.py` — RefreshSchemaView
- Create: `apps/projects/tasks.py` — Celery tasks for schema refresh
- Modify: `config/celery.py`

**Refresh flow:**
1. User POSTs to `/api/workspaces/<workspace_id>/refresh/`
2. View validates role (write+), checks workspace has exactly one tenant (multi-tenant refresh is Phase 9)
3. Creates a new `TenantSchema` record with state=PROVISIONING linked to the tenant
4. Dispatches `refresh_tenant_schema` Celery task with the new schema ID
5. Returns 202 Accepted with `{"schema_id": "<new_schema_id>", "status": "provisioning"}`
6. Task: provisions schema, runs materialization pipeline, marks state=ACTIVE on success; marks FAILED on error
7. On success: update `WorkspaceTenant` to point at the new active schema (or query for it via Tenant); drop old schema
8. On failure: drop the new schema, leave old schema in place

**Status polling endpoint:**
```
GET /api/workspaces/<workspace_id>/refresh/status/
```
Returns: `{"state": "provisioning|active|failed", "started_at": ..., "error": ...}`

**Step 1: Tests**

```python
def test_refresh_returns_202(api_client, workspace, write_user):
    api_client.force_login(write_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 202
    assert resp.data["status"] == "provisioning"

def test_read_only_user_cannot_trigger_refresh(api_client, workspace, read_user):
    api_client.force_login(read_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 403
```

**Step 2: Implement and commit**

```bash
git commit -m "feat: data refresh endpoint with background Celery task"
```

---

### Task 4.2: Schema TTL Celery periodic task

**Files:**
- Modify: `apps/projects/tasks.py`
- Modify: `config/settings/base.py` — add beat schedule

**Task logic:**

```python
# apps/projects/tasks.py
from celery import shared_task
from django.utils import timezone
from datetime import timedelta

SCHEMA_TTL_HOURS = 24

@shared_task
def expire_inactive_schemas():
    """Drop tenant schemas that have had no user activity for TTL hours."""
    cutoff = timezone.now() - timedelta(hours=SCHEMA_TTL_HOURS)
    stale = TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ).select_related("tenant")
    for schema in stale:
        schema.state = SchemaState.TEARDOWN
        schema.save(update_fields=["state"])
        teardown_schema.delay(schema.id)

@shared_task
def teardown_schema(schema_id):
    schema = TenantSchema.objects.get(id=schema_id)
    try:
        SchemaManager().teardown(schema)
        schema.state = SchemaState.EXPIRED
        schema.save(update_fields=["state"])
    except Exception:
        schema.state = SchemaState.ACTIVE  # rollback to safe state
        schema.save(update_fields=["state"])
        raise
```

**Beat schedule:**

```python
# config/settings/base.py
CELERY_BEAT_SCHEDULE = {
    "expire-inactive-schemas": {
        "task": "apps.projects.tasks.expire_inactive_schemas",
        "schedule": crontab(minute="*/30"),  # check every 30 minutes
    },
}
```

**Step 1: Tests**

```python
# tests/test_schema_ttl_task.py
import freezegun

def test_expire_inactive_schemas_drops_stale_schema(tenant_schema):
    tenant_schema.last_accessed_at = timezone.now() - timedelta(hours=25)
    tenant_schema.state = SchemaState.ACTIVE
    tenant_schema.save()

    with patch("apps.projects.tasks.SchemaManager.teardown") as mock_teardown:
        expire_inactive_schemas()

    tenant_schema.refresh_from_db()
    assert tenant_schema.state == SchemaState.EXPIRED

def test_active_schema_not_expired_if_recently_accessed(tenant_schema):
    tenant_schema.last_accessed_at = timezone.now() - timedelta(hours=1)
    tenant_schema.state = SchemaState.ACTIVE
    tenant_schema.save()

    expire_inactive_schemas()
    tenant_schema.refresh_from_db()
    assert tenant_schema.state == SchemaState.ACTIVE
```

**Step 2: Commit**

```bash
git commit -m "feat: Celery task to expire inactive tenant schemas after 24h"
```

---

### Task 4.3: Workspace data unavailability state

When a workspace's underlying schema is EXPIRED, workspace detail endpoint must indicate data is unavailable.

**Files:**
- Modify: `apps/projects/api/workspace_views.py`

Add `schema_status` to workspace serializer:

```python
def get_schema_status(self, obj) -> str:
    """Returns 'available', 'provisioning', or 'unavailable'."""
    tenants = obj.tenants.all()
    schemas = TenantSchema.objects.filter(tenant__in=tenants, state=SchemaState.ACTIVE)
    if schemas.count() == tenants.count():
        return "available"
    provisioning = TenantSchema.objects.filter(tenant__in=tenants, state__in=[SchemaState.PROVISIONING, SchemaState.MATERIALIZING])
    if provisioning.exists():
        return "provisioning"
    return "unavailable"
```

When schema_status is `unavailable`, data-accessing endpoints (data-dictionary, artifact queries, recipe runs) return 503 with `{"error": "Data unavailable. Please refresh workspace data.", "schema_status": "unavailable"}`.

**Commit:** `git commit -m "feat: workspace schema_status field; 503 when schema unavailable"`

---

## Phase 5: Invitations

---

### Task 5.1: WorkspaceInvitation model

**Files:**
- Modify: `apps/projects/models.py`
- Create: migration

```python
class InvitationStatus(models.TextChoices):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"
    FAILED = "failed"

class WorkspaceInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    inviter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_invitations")
    invitee_email = models.EmailField()
    role = models.CharField(max_length=20, choices=WorkspaceRole.choices)
    status = models.CharField(max_length=20, choices=InvitationStatus.choices, default=InvitationStatus.PENDING)
    error_message = models.TextField(blank=True)
    token = models.CharField(max_length=64, unique=True)  # secrets.token_urlsafe(32)
    expires_at = models.DateTimeField()  # created_at + 7 days
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
```

---

### Task 5.2: Invitation CRUD endpoints

```
POST   /api/workspaces/<workspace_id>/invitations/          — send (manage only)
GET    /api/workspaces/<workspace_id>/invitations/          — list (manage only)
DELETE /api/workspaces/<workspace_id>/invitations/<id>/     — revoke (manage only)
POST   /api/invitations/<token>/accept/                     — accept (any authed user)
```

**Send invitation logic:**
- Validate inviter has manage role
- Generate token, set expires_at = now + 7 days
- Send email (use Django's `send_mail` with `EMAIL_BACKEND`)
- If invitee already has a Scout account, the invitation also appears in their UI

**Accept invitation logic:**
- Validate token is valid, not expired, not revoked
- Validate invitee is authenticated and has OAuth connection for all tenants in the workspace
- Create `WorkspaceMembership` with the invitation's role
- Mark invitation as `ACCEPTED`
- If validation fails: mark as `FAILED`, set `error_message`, notify inviter

**Key tests:**

```python
def test_invitation_expires_after_7_days(workspace, manage_user):
    invite = create_invitation(workspace, manage_user, "test@example.com")
    with freeze_time(timezone.now() + timedelta(days=8)):
        resp = accept_invitation(invite.token)
    assert resp.status_code == 400
    assert "expired" in resp.data["error"].lower()

def test_inviter_loses_manage_role_revokes_pending_invitations(workspace, manage_user, invite):
    # Demote manage_user to read_write
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=manage_user)
    membership.role = WorkspaceRole.READ_WRITE
    membership.save()
    invite.refresh_from_db()
    assert invite.status == InvitationStatus.REVOKED

def test_invitation_error_does_not_reveal_tenant_details(workspace, manage_user):
    # Invite someone who lacks tenant access
    resp = accept_invitation_as_user_without_tenant_access(...)
    assert resp.status_code == 400
    # Should not name specific tenants
    assert "commcare" not in resp.data["error"].lower()
```

**Commit:** `git commit -m "feat: workspace invitation system"`

---

### Task 5.3: Invitation expiration Celery task

```python
@shared_task
def expire_pending_invitations():
    WorkspaceInvitation.objects.filter(
        status=InvitationStatus.PENDING,
        expires_at__lt=timezone.now(),
    ).update(status=InvitationStatus.EXPIRED)
```

Add to `CELERY_BEAT_SCHEDULE` to run hourly.

**Commit:** `git commit -m "feat: Celery task to expire pending invitations"`

---

## Phase 6: Audit Log

---

### Task 6.1: AuditLog model and signal infrastructure

**Files:**
- Modify: `apps/projects/models.py`

```python
class AuditAction(models.TextChoices):
    INVITATION_SENT = "invitation_sent"
    INVITATION_ACCEPTED = "invitation_accepted"
    INVITATION_REVOKED = "invitation_revoked"
    ROLE_CHANGED = "role_changed"
    MEMBER_REMOVED = "member_removed"
    WORKSPACE_DELETED = "workspace_deleted"
    DATA_REFRESH_TRIGGERED = "data_refresh_triggered"
    ARTIFACT_DELETED = "artifact_deleted"
    RECIPE_DELETED = "recipe_deleted"
    TENANT_ADDED = "tenant_added"
    TENANT_REMOVED = "tenant_removed"

class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.SET_NULL,  # retain log after workspace deletion
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=50, choices=AuditAction.choices)
    actor_id = models.UUIDField()       # stored as raw ID so it survives user deletion
    actor_email = models.EmailField()   # denormalised for display after user deletion
    actor_name = models.CharField(max_length=255)
    target_user_id = models.UUIDField(null=True, blank=True)
    target_user_email = models.EmailField(blank=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
```

**Note:** Actor identity is fully denormalised (id + email + name) at write time so it remains meaningful after account deletion. This is the spec's deliberate exception to the "Deleted user" display policy.

**Utility function:**

```python
# apps/projects/audit.py
def log_action(workspace, actor, action, target_user=None, details=None):
    AuditLog.objects.create(
        workspace=workspace,
        action=action,
        actor_id=actor.id,
        actor_email=actor.email,
        actor_name=actor.get_full_name(),
        target_user_id=target_user.id if target_user else None,
        target_user_email=target_user.email if target_user else "",
        details=details or {},
    )
```

Call `log_action()` at every action listed in `AuditAction` (in their respective views/services).

---

### Task 6.2: Audit log API

```
GET /api/workspaces/<workspace_id>/audit-log/    — manage only; filtered, paginated
GET /api/audit-log/                              — superuser only; all workspaces
```

Supports query params: `action=`, `actor_email=`, `from_date=`, `to_date=`, `page=`.

Export: `GET /api/workspaces/<workspace_id>/audit-log/?format=csv`

**Commit:** `git commit -m "feat: audit log model, hooks, and API"`

---

## Phase 7: Account Deletion

---

### Task 7.1: Account deletion endpoint

```
DELETE /api/auth/account/
```

**Deletion flow:**
1. Check if user is the sole manage-role member of any multi-member workspaces → if yes, return 400 listing those workspaces; user must assign replacement managers first
2. Delete all of the user's sole-member workspaces (normal workspace deletion cascade)
3. Remove user from all shared workspaces (member removal cascade: delete their threads, keep artifacts/recipes)
4. Revoke all pending sent and received invitations
5. Mark `User.is_active = False` and anonymize PII (email → `deleted_<id>@deleted.scout`, name fields cleared) — do NOT hard-delete, to preserve audit log linkage
6. Audit logs remain intact with the user's real identity (already denormalised at write time)

**Step 1: Tests**

```python
def test_cannot_delete_account_if_sole_manager_of_shared_workspace(api_client, user, shared_workspace):
    resp = api_client.delete("/api/auth/account/")
    assert resp.status_code == 400
    assert str(shared_workspace.id) in resp.data["workspaces_requiring_manager"]

def test_deletion_preserves_artifacts_with_deleted_user_attribution(api_client, user, workspace, artifact):
    api_client.delete("/api/auth/account/")
    artifact.refresh_from_db()
    assert artifact.created_by is None  # SET_NULL
    # Serializer should show "Deleted user"

def test_deletion_removes_users_threads(api_client, user, workspace, thread):
    api_client.delete("/api/auth/account/")
    assert not Thread.objects.filter(id=thread.id).exists()

def test_audit_logs_retain_real_identity_after_deletion(api_client, user, workspace):
    # Trigger an audited action first
    log_action(workspace, user, AuditAction.DATA_REFRESH_TRIGGERED)
    api_client.delete("/api/auth/account/")
    log = AuditLog.objects.filter(workspace=workspace).first()
    assert log.actor_email == user.email  # not anonymized
```

**Commit:** `git commit -m "feat: account deletion endpoint with cascade and sole-manager protection"`

---

## Phase 8: Thread Named-User Sharing

---

### Task 8.1: ThreadAccess model and named-user grants

**Files:**
- Modify: `apps/chat/models.py`

```python
class ThreadAccess(models.Model):
    """Grants a specific named Scout user read-only access to a thread."""
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="access_grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="thread_access_grants")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["thread", "user"]]
```

**Endpoints:**
```
POST   /api/workspaces/<workspace_id>/threads/<thread_id>/access/       — manage only; grant named user
DELETE /api/workspaces/<workspace_id>/threads/<thread_id>/access/<id>/  — manage only; revoke
```

**Access precedence:** When checking if a user can read a thread, check workspace membership first; `ThreadAccess` grants read-only access only for non-members.

**Commit:** `git commit -m "feat: thread named-user access grants"`

---

## Phase 9: Multi-Tenant Workspaces

This phase builds the multi-tenant workspace capability on top of all the previous work. The foundation (`WorkspaceTenant`, multi-tenant-capable models) was laid in Phase 2; this phase activates the database view schema layer.

---

### Task 9.1: Workspace tenant add/remove (manage only)

```
POST   /api/workspaces/<workspace_id>/tenants/      — add tenant (manage only)
DELETE /api/workspaces/<workspace_id>/tenants/<id>/ — remove tenant (manage only)
```

Adding or removing a tenant triggers a view schema rebuild (Task 9.2).

**Validation:** User must have TenantMembership for the tenant being added. All workspace members must also have access — or show which members would lose access and block until they're removed.

---

### Task 9.2: View schema build and rebuild

A multi-tenant workspace needs a PostgreSQL schema containing views that JOIN data from the individual per-tenant schemas.

**New model:**

```python
class WorkspaceViewSchema(models.Model):
    """Tracks the PostgreSQL view schema for a multi-tenant workspace."""
    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="view_schema")
    schema_name = models.CharField(max_length=255, unique=True)
    state = models.CharField(max_length=20, choices=SchemaState.choices)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

**View schema TTL** is independent of the underlying tenant schema TTLs. The workspace view schema expires if the workspace itself goes inactive for 24 hours. Touching `WorkspaceViewSchema.last_accessed_at` follows the same rules as `TenantSchema` (user-initiated actions only).

**Recovery logic:**
- View schema expired but underlying tenant schemas still alive → view rebuild only (no re-fetch from external system)
- View schema expired AND some tenant schemas also expired → re-fetch those tenants first, then rebuild views

**Celery task:** `rebuild_workspace_view_schema(workspace_id)` — builds views joining per-tenant schemas; cuts over on success; stays on old view schema on failure.

**MCP server changes:** Add tenant routing to multi-tenant workspace queries. The agent passes `workspace_id`; the MCP server resolves which schema to use (view schema for multi-tenant, direct tenant schema for single-tenant).

---

## Phase 10: Tenant Access Validation Checkpoints

---

### Task 10.1: Validation at workspace switch and creation

Currently validation only runs at OAuth login. Add checkpoints at:
- New workspace creation (validate all tenant memberships)
- Workspace switch in UI (`POST /api/workspaces/<id>/select/`)
- Invitation acceptance

**24h validation token:** Add `last_validated_at` to `TenantMembership`. Skip external re-validation if `last_validated_at > now - 24h`. If re-validation fails with 5xx, mark user as suspended on that tenant.

---

### Task 10.2: Suspension handling

```python
class TenantMembership(models.Model):
    # ...
    is_suspended = models.BooleanField(default=False)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True)
    last_validated_at = models.DateTimeField(null=True, blank=True)
```

Suspended users see an error on all workspace operations for affected tenants. Suspension does NOT reset TTL on the tenant schema. On successful retry, clear `is_suspended` and require re-authentication.

---

## Testing Strategy

Every task uses the same pattern:
1. Write failing test(s) that express the spec requirement
2. Run to confirm failure
3. Implement minimum code to pass
4. Run to confirm passing
5. Commit

**Test fixtures to create up-front** (put in `tests/conftest.py`):

```python
@pytest.fixture
def tenant(db):
    return Tenant.objects.create(provider="commcare", external_id="test-domain", canonical_name="Test Domain")

@pytest.fixture
def user(db):
    return User.objects.create_user(email="user@example.com", password="pass", first_name="Test", last_name="User")

@pytest.fixture
def workspace(db, user, tenant):
    ws = Workspace.objects.create(name="Test Workspace", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    return ws

@pytest.fixture
def read_user(db, workspace):
    u = User.objects.create_user(email="reader@example.com", password="pass")
    WorkspaceMembership.objects.create(workspace=workspace, user=u, role=WorkspaceRole.READ)
    return u

@pytest.fixture
def write_user(db, workspace):
    u = User.objects.create_user(email="writer@example.com", password="pass")
    WorkspaceMembership.objects.create(workspace=workspace, user=u, role=WorkspaceRole.READ_WRITE)
    return u
```

---

## Key Files Reference

| What to build | Where |
|---|---|
| Workspace, WorkspaceTenant, WorkspaceMembership models | `apps/projects/models.py` |
| WorkspaceInvitation, AuditLog models | `apps/projects/models.py` |
| WorkspaceViewSchema model | `apps/projects/models.py` |
| DRF permission classes | `apps/projects/permissions.py` |
| Audit log utility | `apps/projects/audit.py` |
| Workspace API views | `apps/projects/api/workspace_views.py` |
| Celery tasks | `apps/projects/tasks.py` |
| TenantMembership auto-create workspace signal | `apps/users/signals.py` |
| Common utilities (creator_display_name) | `apps/common/utils.py` |
| Soft delete manager | `apps/artifacts/models.py`, `apps/recipes/models.py` |
| ThreadAccess model | `apps/chat/models.py` |
| Updated workspace resolver | `apps/projects/workspace_resolver.py` |
| Beat schedule | `config/settings/base.py` |

---

## What This Plan Does NOT Include

These are in the spec but deliberately deferred:

- **Inline OAuth re-auth popup/modal** (Phase 10.2 does suspension; the UI re-auth flow is a frontend task)
- **Auto-promotion of longest-standing write-role member when last manager is auto-removed** (mentioned in Phase 3.3 member management but the auto-detect-and-promote loop is part of the background tenant validation task in Phase 10)
- **Rate-limited refresh queue** ("refresh queued" vs "refresh in progress" distinction) — basic Celery queuing is sufficient initially; fine-grained rate limits are a separate task
