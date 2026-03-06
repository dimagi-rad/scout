# Custom Workspace — Revised Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Introduce CustomWorkspace — a user-created workspace grouping multiple CommCare tenants — by evolving the existing `TenantWorkspace` model rather than introducing a parallel hierarchy.

**Architecture summary (from plan review, 2026-03-05):**

| Decision | Choice |
|---|---|
| App rename | Deferred — keep `apps/projects` |
| Model design | Evolve `TenantWorkspace` in-place: OneToOne → M2M tenants, add `workspace_type` discriminator |
| Access control | Unified `WorkspaceMembership` for all workspace types; synced from `TenantMembership` via signal |
| Transport | `workspace_id` in URL path (aligned with parallel URL refactor) |
| Permission enforcement | DRF `WorkspacePermission` permission class |

**Assumption:** A parallel refactor is in progress that adds `workspace_id` to URL paths (e.g., `/api/knowledge/<workspace_id>/`). This plan's Phase 3 `WorkspacePermission` class slots into that URL structure. Phases 1–2 are independent and can proceed before the URL refactor lands.

---

## Callsite impact summary

Switching `TenantWorkspace.tenant` from `OneToOneField` to M2M affects these production callsites:

| File | Line(s) | Change required |
|---|---|---|
| `apps/projects/models.py` | 98–111 | Remove `tenant` FK, add M2M, update `__str__`, update `tenant_id`/`tenant_name` properties, fix `Meta.ordering` |
| `apps/projects/api/views.py` | 37–39 | `get_or_create(tenant=...)` → `TenantWorkspace.get_or_create_for_tenant(tenant)` |
| `apps/recipes/models.py` | 104 | `self.workspace.tenant.canonical_name` → `self.workspace.name` |
| `apps/recipes/api/views.py` | 46 | `TenantWorkspace.objects.get_or_create(tenant=...)` → `TenantWorkspace.get_or_create_for_tenant(tenant)` |
| `apps/recipes/services/runner.py` | 106 | `workspace.tenant` → `workspace.primary_tenant()` |
| `apps/artifacts/models.py` | 331 | `workspace.tenant` → `workspace.primary_tenant()` |
| `apps/artifacts/views.py` | 642, 698, 765, 777, 855, 914, 998, 1063 | `workspace.tenant` → `workspace.primary_tenant()` |
| `apps/knowledge/api/views.py` | 57 | `tenant=membership.tenant` (check context) |
| `tests/conftest.py` | 62 | `TenantWorkspace.objects.create(tenant=...)` → `TenantWorkspace.create_for_tenant(tenant)` |
| `tests/test_models.py` | 135, 147, 149 | Same |
| `tests/test_artifacts.py` | 401 | Same |
| `apps/artifacts/tests/test_artifact_query_data.py` | 25 | Same |
| `apps/artifacts/tests/test_share_api.py` | 51 | Same |

---

## Phase 1: Evolve TenantWorkspace

### Task 1: Write tests for TenantWorkspace model evolution

**Files:**
- Create: `tests/test_workspace_evolution.py`

**Step 1: Write the failing tests**

```python
import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant

User = get_user_model()


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(
        provider="commcare", external_id="domain-a", canonical_name="Domain A"
    )


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(
        provider="commcare", external_id="domain-b", canonical_name="Domain B"
    )


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@test.com", password="pass")


class TestTenantWorkspaceEvolution:
    def test_create_for_tenant_creates_workspace_and_links_tenant(self, tenant_a):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.create_for_tenant(tenant_a)
        assert ws.workspace_type == "tenant"
        assert ws.name == "Domain A"
        assert list(ws.tenants.all()) == [tenant_a]

    def test_primary_tenant_returns_sole_tenant(self, tenant_a):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.create_for_tenant(tenant_a)
        assert ws.primary_tenant() == tenant_a

    def test_tenant_id_property_works_via_primary_tenant(self, tenant_a):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.create_for_tenant(tenant_a)
        assert ws.tenant_id == "domain-a"

    def test_tenant_name_property_works_via_primary_tenant(self, tenant_a):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.create_for_tenant(tenant_a)
        assert ws.tenant_name == "Domain A"

    def test_create_custom_workspace(self, owner):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.objects.create(
            workspace_type="custom",
            name="My Custom Workspace",
            created_by=owner,
        )
        assert ws.workspace_type == "custom"
        assert ws.name == "My Custom Workspace"

    def test_custom_workspace_add_tenants(self, owner, tenant_a, tenant_b):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.objects.create(
            workspace_type="custom", name="Multi-tenant", created_by=owner
        )
        ws.tenants.add(tenant_a, tenant_b)
        assert ws.tenants.count() == 2

    def test_primary_tenant_raises_for_custom_workspace(self, owner, tenant_a):
        from apps.projects.models import TenantWorkspace

        ws = TenantWorkspace.objects.create(
            workspace_type="custom", name="Custom", created_by=owner
        )
        ws.tenants.add(tenant_a)
        with pytest.raises(ValueError, match="primary_tenant"):
            ws.primary_tenant()

    def test_get_or_create_for_tenant_idempotent(self, tenant_a):
        from apps.projects.models import TenantWorkspace

        ws1, created1 = TenantWorkspace.get_or_create_for_tenant(tenant_a)
        ws2, created2 = TenantWorkspace.get_or_create_for_tenant(tenant_a)
        assert ws1.id == ws2.id
        assert created1 is True
        assert created2 is False
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/test_workspace_evolution.py -x -v
```

Expected: FAIL — `workspace_type`, `tenants`, `create_for_tenant` don't exist yet.

---

### Task 2: Implement model changes and migrations

**Files:**
- Modify: `apps/projects/models.py`

**Step 1: Replace the `tenant` OneToOneField with M2M + add new fields**

Replace in `apps/projects/models.py`:

```python
import uuid

from django.conf import settings
from django.db import models
from django_pydantic_field import SchemaField


class WorkspaceType(models.TextChoices):
    TENANT = "tenant", "Tenant Workspace"
    CUSTOM = "custom", "Custom Workspace"


class WorkspaceTenant(models.Model):
    """Links a TenantWorkspace to the Tenant(s) it represents."""

    workspace = models.ForeignKey(
        "TenantWorkspace",
        on_delete=models.CASCADE,
        related_name="workspace_tenants",
    )
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace_links",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["workspace", "tenant"]

    def __str__(self):
        return f"{self.workspace.name} ← {self.tenant.canonical_name}"


class TenantWorkspace(models.Model):
    """Workspace holding agent config. Type 'tenant' wraps one CommCare tenant;
    type 'custom' groups multiple tenants and is user-created."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    workspace_type = models.CharField(
        max_length=20,
        choices=WorkspaceType.choices,
        default=WorkspaceType.TENANT,
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Display name. Auto-populated from tenant.canonical_name for type='tenant'.",
    )
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_workspaces",
        help_text="Set for type='custom'. Null for auto-created tenant workspaces.",
    )

    # Tenant relationships — managed via WorkspaceTenant through table.
    # type='tenant': exactly one tenant.
    # type='custom': one or more tenants.
    tenants = models.ManyToManyField(
        "users.Tenant",
        through=WorkspaceTenant,
        related_name="workspaces",
        blank=True,
    )

    system_prompt = models.TextField(
        blank=True,
        help_text="Workspace-level system prompt. Merged with the base agent prompt.",
    )
    data_dictionary = models.JSONField(
        null=True,
        blank=True,
        help_text="Auto-generated schema documentation. Populated for type='tenant' only.",
    )
    data_dictionary_generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name or f"Workspace {self.id}"

    # --- Factory methods ---

    @classmethod
    def create_for_tenant(cls, tenant, **kwargs):
        """Create a type='tenant' workspace for the given Tenant and link it."""
        ws = cls.objects.create(
            workspace_type=WorkspaceType.TENANT,
            name=tenant.canonical_name,
            **kwargs,
        )
        ws.tenants.add(tenant)
        return ws

    @classmethod
    def get_or_create_for_tenant(cls, tenant):
        """Get or create the type='tenant' workspace for the given Tenant.

        Returns (workspace, created) matching the get_or_create convention.
        """
        existing = cls.objects.filter(
            workspace_tenants__tenant=tenant,
            workspace_type=WorkspaceType.TENANT,
        ).first()
        if existing:
            return existing, False
        return cls.create_for_tenant(tenant), True

    # --- Backward-compat accessors for type='tenant' workspaces ---

    def primary_tenant(self):
        """Return the single Tenant for type='tenant' workspaces.

        Raises ValueError for type='custom' workspaces — those have multiple
        tenants; callers should use .tenants.all() instead.
        """
        if self.workspace_type == WorkspaceType.CUSTOM:
            raise ValueError(
                f"primary_tenant() called on a custom workspace ({self.id}). "
                "Use .tenants.all() to get all tenants."
            )
        return self.tenants.select_related().get()

    @property
    def tenant_id(self):
        """Convenience: external_id of the sole tenant. type='tenant' only."""
        return self.primary_tenant().external_id

    @property
    def tenant_name(self):
        """Convenience: canonical_name of the sole tenant. type='tenant' only."""
        return self.primary_tenant().canonical_name
```

**Step 2: Generate migrations**

Three migrations, in order:

```bash
# Migration A: add new fields + WorkspaceTenant table (schema only)
uv run python manage.py makemigrations projects --name add_workspace_type_and_tenant_m2m --empty
```

Write this migration manually to:
1. Add `workspace_type`, `name`, `description`, `created_by` fields to `TenantWorkspace`
2. Create the `WorkspaceTenant` table
3. Data migration: for each existing `TenantWorkspace`, create a `WorkspaceTenant` record and populate `name` from `tenant.canonical_name`
4. Remove the `tenant` OneToOneField from `TenantWorkspace`

The data migration step (3) must run before step (4). Write it as a single migration with both `RunPython` and `RemoveField` operations in the correct order:

```python
from django.db import migrations, models
import django.db.models.deletion
import uuid


def populate_workspace_tenants(apps, schema_editor):
    TenantWorkspace = apps.get_model("projects", "TenantWorkspace")
    WorkspaceTenant = apps.get_model("projects", "WorkspaceTenant")
    for ws in TenantWorkspace.objects.select_related("tenant").all():
        WorkspaceTenant.objects.create(workspace=ws, tenant=ws.tenant)
        TenantWorkspace.objects.filter(pk=ws.pk).update(name=ws.tenant.canonical_name)


def reverse_populate(apps, schema_editor):
    WorkspaceTenant = apps.get_model("projects", "WorkspaceTenant")
    WorkspaceTenant.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0015_add_discovering_state"),  # adjust to actual last migration
        ("users", "0006_tenantmembership_use_tenant_fk"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        # 1. Add new fields to TenantWorkspace
        migrations.AddField(
            model_name="tenantworkspace",
            name="workspace_type",
            field=models.CharField(
                choices=[("tenant", "Tenant Workspace"), ("custom", "Custom Workspace")],
                default="tenant",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="tenantworkspace",
            name="name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="tenantworkspace",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="tenantworkspace",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_workspaces",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # 2. Create WorkspaceTenant table
        migrations.CreateModel(
            name="WorkspaceTenant",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_tenants",
                        to="projects.tenantworkspace",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_links",
                        to="users.tenant",
                    ),
                ),
                ("added_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"unique_together": {("workspace", "tenant")}},
        ),
        # 3. Populate WorkspaceTenant from existing OneToOne data
        migrations.RunPython(populate_workspace_tenants, reverse_populate),
        # 4. Remove the old OneToOne tenant field
        migrations.RemoveField(model_name="tenantworkspace", name="tenant"),
        # 5. Fix ordering (was tenant__canonical_name, now name)
        migrations.AlterModelOptions(
            name="tenantworkspace",
            options={"ordering": ["name"]},
        ),
    ]
```

**Step 3: Run migration**

```bash
uv run python manage.py migrate
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_workspace_evolution.py -x -v
```

Expected: All pass.

---

### Task 3: Update all callsites

**Files to modify (see callsite impact table at top):**

Update every `TenantWorkspace.objects.create(tenant=...)` and `TenantWorkspace.objects.get_or_create(tenant=...)` callsite to use the new factory methods. Update every `workspace.tenant` access outside of `apps/projects/models.py` to use either `workspace.primary_tenant()` or `workspace.name`.

**Systematic changes:**

1. `workspace.tenant.canonical_name` → `workspace.name` (wherever display name is needed)
2. `workspace.tenant.external_id` → `workspace.primary_tenant().external_id` (wherever the CommCare domain ID is needed for SQL queries)
3. `workspace.tenant` passed as an object → `workspace.primary_tenant()` (e.g., `runner.py:106`, `artifacts/models.py:331`, `artifacts/views.py`)
4. `TenantWorkspace.objects.create(tenant=...)` → `TenantWorkspace.create_for_tenant(...)`
5. `TenantWorkspace.objects.get_or_create(tenant=...)` → `TenantWorkspace.get_or_create_for_tenant(...)`

After updating:

```bash
uv run pytest -x -q
uv run ruff check . && uv run ruff format .
```

Expected: All existing tests pass.

**Step: Commit**

```bash
git add -A && git commit -m "refactor: evolve TenantWorkspace — OneToOne→M2M tenants, add workspace_type and factory methods"
```

---

## Phase 2: WorkspaceMembership with TenantMembership sync

### Task 4: Write tests for WorkspaceMembership

**Files:**
- Create: `tests/test_workspace_membership.py`

```python
import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant, TenantMembership
from apps.projects.models import TenantWorkspace

User = get_user_model()


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="domain-a", canonical_name="Domain A"
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="user@test.com", password="pass")


@pytest.fixture
def workspace(tenant):
    return TenantWorkspace.create_for_tenant(tenant)


class TestWorkspaceMembership:
    def test_create_membership(self, workspace, user):
        from apps.projects.models import WorkspaceMembership

        m = WorkspaceMembership.objects.create(workspace=workspace, user=user, role="editor")
        assert m.role == "editor"
        assert workspace.memberships.count() == 1

    def test_duplicate_membership_rejected(self, workspace, user):
        from django.db import IntegrityError

        from apps.projects.models import WorkspaceMembership

        WorkspaceMembership.objects.create(workspace=workspace, user=user, role="editor")
        with pytest.raises(IntegrityError):
            WorkspaceMembership.objects.create(workspace=workspace, user=user, role="viewer")

    def test_role_choices(self, workspace, user):
        from apps.projects.models import WorkspaceMembership

        for role in ["owner", "editor", "viewer"]:
            ws2 = TenantWorkspace.objects.create(workspace_type="tenant", name=f"WS {role}")
            m = WorkspaceMembership.objects.create(workspace=ws2, user=user, role=role)
            assert m.role == role


class TestTenantMembershipSignal:
    """TenantMembership post_save/post_delete must keep WorkspaceMembership in sync."""

    def test_creating_tenant_membership_creates_workspace_membership(self, tenant, user, workspace):
        from apps.projects.models import WorkspaceMembership

        TenantMembership.objects.create(user=user, tenant=tenant)
        assert WorkspaceMembership.objects.filter(
            workspace=workspace, user=user, role="editor"
        ).exists()

    def test_deleting_tenant_membership_removes_workspace_membership(self, tenant, user, workspace):
        from apps.projects.models import WorkspaceMembership

        tm = TenantMembership.objects.create(user=user, tenant=tenant)
        assert WorkspaceMembership.objects.filter(workspace=workspace, user=user).exists()

        tm.delete()
        assert not WorkspaceMembership.objects.filter(workspace=workspace, user=user).exists()

    def test_signal_creates_workspace_if_none_exists(self, tenant, user):
        """Signal creates the workspace if it doesn't exist yet, then creates membership."""
        from apps.projects.models import WorkspaceMembership

        # No workspace exists for tenant yet
        TenantMembership.objects.create(user=user, tenant=tenant)
        ws = TenantWorkspace.objects.filter(
            workspace_tenants__tenant=tenant, workspace_type="tenant"
        ).get()
        assert WorkspaceMembership.objects.filter(workspace=ws, user=user).exists()
```

**Run to verify failure:**

```bash
uv run pytest tests/test_workspace_membership.py -x -v
```

Expected: FAIL — `WorkspaceMembership` doesn't exist yet.

---

### Task 5: Implement WorkspaceMembership model, signal, and migration

**Files:**
- Modify: `apps/projects/models.py`
- Create: `apps/projects/signals.py`
- Modify: `apps/projects/apps.py`

**Step 1: Add WorkspaceMembership to models.py**

Add after `TenantWorkspace`:

```python
class WorkspaceMembership(models.Model):
    """Role-based membership for any workspace type.

    For type='tenant' workspaces, memberships are auto-synced from TenantMembership
    via signal (see apps/projects/signals.py). Direct creation is only needed for
    type='custom' workspaces.
    """

    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("editor", "Editor"),
        ("viewer", "Viewer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        TenantWorkspace,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_invitations",
        help_text="Null for auto-synced tenant workspace memberships.",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["workspace", "user"]

    def __str__(self):
        return f"{self.user.email} — {self.role} in {self.workspace.name}"
```

**Step 2: Create `apps/projects/signals.py`**

```python
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


@receiver(post_save, sender="users.TenantMembership")
def sync_workspace_membership_on_create(sender, instance, created, **kwargs):
    """Create a WorkspaceMembership when a TenantMembership is created.

    Gets or creates the TenantWorkspace for the tenant, then creates a
    WorkspaceMembership with role='editor'. Idempotent: get_or_create means
    re-running OAuth login doesn't create duplicates.
    """
    if not created:
        return

    from apps.projects.models import TenantWorkspace, WorkspaceMembership

    workspace, _ = TenantWorkspace.get_or_create_for_tenant(instance.tenant)
    WorkspaceMembership.objects.get_or_create(
        workspace=workspace,
        user=instance.user,
        defaults={"role": "editor"},
    )


@receiver(post_delete, sender="users.TenantMembership")
def sync_workspace_membership_on_delete(sender, instance, **kwargs):
    """Remove WorkspaceMembership when TenantMembership is deleted.

    Only removes membership from type='tenant' workspaces. Custom workspace
    memberships are managed explicitly and are not auto-removed.
    """
    from apps.projects.models import TenantWorkspace, WorkspaceMembership

    workspace = (
        TenantWorkspace.objects.filter(
            workspace_tenants__tenant=instance.tenant,
            workspace_type="tenant",
        ).first()
    )
    if workspace:
        WorkspaceMembership.objects.filter(workspace=workspace, user=instance.user).delete()
```

**Step 3: Register signals in `apps/projects/apps.py`**

```python
from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.projects"

    def ready(self):
        import apps.projects.signals  # noqa: F401
```

**Step 4: Generate and run migration**

```bash
uv run python manage.py makemigrations projects --name add_workspace_membership
uv run python manage.py migrate
```

Write a data migration after the schema migration to create `WorkspaceMembership` rows for all existing `TenantMembership` records:

```python
def backfill_workspace_memberships(apps, schema_editor):
    TenantMembership = apps.get_model("users", "TenantMembership")
    TenantWorkspace = apps.get_model("projects", "TenantWorkspace")
    WorkspaceMembership = apps.get_model("projects", "WorkspaceMembership")

    for tm in TenantMembership.objects.select_related("tenant", "user").all():
        workspace = TenantWorkspace.objects.filter(
            workspace_tenants__tenant=tm.tenant,
            workspace_type="tenant",
        ).first()
        if workspace:
            WorkspaceMembership.objects.get_or_create(
                workspace=workspace,
                user=tm.user,
                defaults={"role": "editor"},
            )
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_workspace_membership.py -x -v
uv run pytest -x -q  # full suite
```

**Step 6: Commit**

```bash
git add -A && git commit -m "feat: add WorkspaceMembership model and TenantMembership sync signal"
```

---

## Phase 3: DRF WorkspacePermission

**Dependency:** Requires `workspace_id` to be present in URL kwargs. This phase slots into the URL refactor that changes `/api/knowledge/<tenant_id>/` to `/api/knowledge/<workspace_id>/`. If that refactor is not yet merged, this phase can be done in a feature branch and merged after.

### Task 6: Write tests for WorkspacePermission

**Files:**
- Create: `tests/test_workspace_permission.py`

```python
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from rest_framework.request import Request
from unittest.mock import MagicMock

from apps.users.models import Tenant, TenantMembership
from apps.projects.models import TenantWorkspace, WorkspaceMembership

User = get_user_model()


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="domain-a", canonical_name="Domain A"
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(email="perm_test@test.com", password="pass")


@pytest.fixture
def workspace(tenant):
    return TenantWorkspace.create_for_tenant(tenant)


def _make_request(user, workspace_id=None):
    factory = RequestFactory()
    django_req = factory.get("/")
    django_req.user = user
    req = Request(django_req)
    return req


def _make_view(workspace_id=None):
    view = MagicMock()
    view.kwargs = {"workspace_id": str(workspace_id)} if workspace_id else {}
    return view


class TestWorkspacePermission:
    """Pure permission logic tests — no HTTP round-trips."""

    def test_non_workspace_scoped_view_passes(self, user):
        from apps.projects.permissions import WorkspacePermission

        perm = WorkspacePermission()
        req = _make_request(user)
        assert perm.has_permission(req, _make_view()) is True

    @pytest.mark.django_db
    def test_member_with_editor_role_passes(self, workspace, user):
        from apps.projects.permissions import WorkspacePermission

        WorkspaceMembership.objects.create(workspace=workspace, user=user, role="editor")
        perm = WorkspacePermission()
        req = _make_request(user)
        assert perm.has_permission(req, _make_view(workspace.id)) is True

    @pytest.mark.django_db
    def test_non_member_denied(self, workspace, user):
        from rest_framework.exceptions import PermissionDenied

        from apps.projects.permissions import WorkspacePermission

        perm = WorkspacePermission()
        req = _make_request(user)
        with pytest.raises(PermissionDenied):
            perm.has_permission(req, _make_view(workspace.id))

    @pytest.mark.django_db
    def test_unknown_workspace_id_raises_404(self, user):
        import uuid
        from rest_framework.exceptions import NotFound

        from apps.projects.permissions import WorkspacePermission

        perm = WorkspacePermission()
        req = _make_request(user)
        with pytest.raises(NotFound):
            perm.has_permission(req, _make_view(uuid.uuid4()))

    @pytest.mark.django_db
    def test_permission_caches_workspace_on_request(self, workspace, user):
        from apps.projects.permissions import WorkspacePermission

        WorkspaceMembership.objects.create(workspace=workspace, user=user, role="editor")
        perm = WorkspacePermission()
        req = _make_request(user)
        perm.has_permission(req, _make_view(workspace.id))
        assert req.workspace == workspace

    @pytest.mark.django_db
    def test_custom_workspace_member_without_tenant_access_denied(self, user, tenant):
        """Member of a custom workspace who lost TenantMembership for a component tenant gets 403."""
        from rest_framework.exceptions import PermissionDenied

        from apps.projects.permissions import WorkspacePermission

        ws = TenantWorkspace.objects.create(
            workspace_type="custom", name="Custom", created_by=user
        )
        ws.tenants.add(tenant)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")
        # user does NOT have TenantMembership for tenant

        perm = WorkspacePermission()
        req = _make_request(user)
        with pytest.raises(PermissionDenied, match="tenant access"):
            perm.has_permission(req, _make_view(ws.id))

    @pytest.mark.django_db
    def test_custom_workspace_member_with_full_tenant_access_passes(self, user, tenant):
        from apps.projects.permissions import WorkspacePermission

        ws = TenantWorkspace.objects.create(
            workspace_type="custom", name="Custom", created_by=user
        )
        ws.tenants.add(tenant)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")
        TenantMembership.objects.create(user=user, tenant=tenant)

        perm = WorkspacePermission()
        req = _make_request(user)
        assert perm.has_permission(req, _make_view(ws.id)) is True
```

**Run to verify failure:**

```bash
uv run pytest tests/test_workspace_permission.py -x -v
```

---

### Task 7: Implement WorkspacePermission

**Files:**
- Create: `apps/projects/permissions.py`

```python
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import BasePermission

from apps.projects.models import TenantWorkspace, WorkspaceMembership


def _get_missing_tenant_access(user, workspace):
    """Return list of tenant external_ids the user lacks TenantMembership for.

    Only meaningful for type='custom' workspaces. Returns empty list if the user
    has TenantMembership for every component tenant.
    """
    from apps.users.models import TenantMembership

    workspace_tenant_ids = set(
        workspace.tenants.values_list("external_id", flat=True)
    )
    covered_ids = set(
        TenantMembership.objects.filter(
            user=user, tenant__external_id__in=workspace_tenant_ids
        ).values_list("tenant__external_id", flat=True)
    )
    return list(workspace_tenant_ids - covered_ids)


class WorkspacePermission(BasePermission):
    """Checks WorkspaceMembership for workspace_id in URL kwargs.

    Applied to all workspace-scoped endpoints. For type='custom' workspaces,
    also validates that the user still has TenantMembership for all component
    tenants (re-validated on every request, not just at entry time).

    Sets request.workspace for view use — avoids a second DB fetch in the view.
    """

    def has_permission(self, request, view):
        workspace_id = view.kwargs.get("workspace_id")
        if not workspace_id:
            return True

        workspace = TenantWorkspace.objects.filter(id=workspace_id).first()
        if not workspace:
            raise NotFound("Workspace not found.")

        has_membership = WorkspaceMembership.objects.filter(
            workspace=workspace, user=request.user
        ).exists()
        if not has_membership:
            raise PermissionDenied("Not a member of this workspace.")

        if workspace.workspace_type == "custom":
            missing = _get_missing_tenant_access(request.user, workspace)
            if missing:
                raise PermissionDenied(
                    f"Missing tenant access: {', '.join(missing)}. "
                    "Contact the workspace owner."
                )

        request.workspace = workspace
        return True
```

**Run tests:**

```bash
uv run pytest tests/test_workspace_permission.py -x -v
uv run pytest -x -q
```

**Commit:**

```bash
git add -A && git commit -m "feat: add WorkspacePermission DRF class with tenant-access re-validation for custom workspaces"
```

---

## Phase 4: Custom Workspace CRUD API

### Task 8: Write API tests

**Files:**
- Create: `tests/test_custom_workspace_api.py`

```python
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.models import Tenant, TenantMembership
from apps.projects.models import TenantWorkspace, WorkspaceMembership

User = get_user_model()


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@test.com", password="pass")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@test.com", password="pass")


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(
        provider="commcare", external_id="domain-a", canonical_name="Domain A"
    )


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(
        provider="commcare", external_id="domain-b", canonical_name="Domain B"
    )


@pytest.fixture
def owner_memberships(owner, tenant_a, tenant_b):
    TenantMembership.objects.create(user=owner, tenant=tenant_a)
    TenantMembership.objects.create(user=owner, tenant=tenant_b)


@pytest.fixture
def custom_workspace(owner, tenant_a, tenant_b, owner_memberships):
    ws = TenantWorkspace.objects.create(
        workspace_type="custom", name="Test Workspace", created_by=owner
    )
    ws.tenants.add(tenant_a, tenant_b)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


class TestCustomWorkspaceList:
    def test_lists_only_member_workspaces(self, client, owner, custom_workspace):
        client.force_login(owner)
        response = client.get("/api/custom-workspaces/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Test Workspace"

    def test_excludes_non_member_workspaces(self, client, other_user, custom_workspace):
        client.force_login(other_user)
        response = client.get("/api/custom-workspaces/")
        assert response.status_code == 200
        assert len(response.json()) == 0

    def test_unauthenticated_returns_403(self, client):
        response = client.get("/api/custom-workspaces/")
        assert response.status_code == 403


class TestCustomWorkspaceCreate:
    def test_create_workspace_makes_owner_member(
        self, client, owner, tenant_a, owner_memberships
    ):
        client.force_login(owner)
        response = client.post(
            "/api/custom-workspaces/",
            data={"name": "New WS", "tenant_ids": [str(tenant_a.id)]},
            content_type="application/json",
        )
        assert response.status_code == 201
        ws = TenantWorkspace.objects.get(name="New WS")
        assert ws.workspace_type == "custom"
        assert WorkspaceMembership.objects.filter(
            workspace=ws, user=owner, role="owner"
        ).exists()

    def test_create_blocked_if_missing_tenant_access(self, client, owner, tenant_a, tenant_b):
        # owner only has access to tenant_a, not tenant_b
        TenantMembership.objects.create(user=owner, tenant=tenant_a)
        client.force_login(owner)
        response = client.post(
            "/api/custom-workspaces/",
            data={"name": "Bad WS", "tenant_ids": [str(tenant_a.id), str(tenant_b.id)]},
            content_type="application/json",
        )
        assert response.status_code == 400


class TestCustomWorkspaceEnter:
    def test_enter_returns_workspace_detail(self, client, owner, custom_workspace):
        client.force_login(owner)
        response = client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Workspace"
        assert len(data["tenants"]) == 2

    def test_enter_blocked_when_missing_tenant_access(
        self, client, other_user, custom_workspace
    ):
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        # other_user has no TenantMembership for either domain
        client.force_login(other_user)
        response = client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 403

    def test_enter_blocked_for_non_member(self, client, other_user, custom_workspace):
        client.force_login(other_user)
        response = client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 403


class TestOwnerRoleProtection:
    def test_cannot_invite_with_owner_role(
        self, client, owner, custom_workspace, other_user, owner_memberships
    ):
        TenantMembership.objects.create(user=other_user, tenant=custom_workspace.tenants.first())
        TenantMembership.objects.create(user=other_user, tenant=custom_workspace.tenants.last())
        client.force_login(owner)
        response = client.post(
            f"/api/custom-workspaces/{custom_workspace.id}/members/",
            data={"user_id": str(other_user.id), "role": "owner"},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_cannot_change_role_to_owner(self, client, owner, custom_workspace, other_user):
        membership = WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        client.force_login(owner)
        response = client.patch(
            f"/api/custom-workspaces/{custom_workspace.id}/members/{membership.id}/",
            data={"role": "owner"},
            content_type="application/json",
        )
        assert response.status_code == 400
```

**Run to verify failure:**

```bash
uv run pytest tests/test_custom_workspace_api.py -x -v
```

Expected: FAIL — 404 (URLs not registered).

---

### Task 9: Implement API serializers, views, and URLs

**Files:**
- Create: `apps/projects/api/custom_workspace_serializers.py`
- Modify: `apps/projects/api/views.py` (add new view classes)
- Create: `apps/projects/api/custom_workspace_urls.py`
- Modify: `config/urls.py`

**Step 1: Create serializers**

`apps/projects/api/custom_workspace_serializers.py`:

```python
from rest_framework import serializers

from apps.projects.models import TenantWorkspace, WorkspaceMembership
from apps.users.models import Tenant


class WorkspaceTenantSerializer(serializers.Serializer):
    id = serializers.UUIDField(source="tenant.id", read_only=True)
    external_id = serializers.CharField(source="tenant.external_id", read_only=True)
    name = serializers.CharField(source="tenant.canonical_name", read_only=True)
    added_at = serializers.DateTimeField(read_only=True)


class WorkspaceMemberSerializer(serializers.ModelSerializer):
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
        model = TenantWorkspace
        fields = ["id", "name", "description", "created_at", "updated_at",
                  "tenant_count", "member_count", "role"]


class CustomWorkspaceDetailSerializer(serializers.ModelSerializer):
    tenants = WorkspaceTenantSerializer(
        source="workspace_tenants", many=True, read_only=True
    )
    members = WorkspaceMemberSerializer(source="memberships", many=True, read_only=True)

    class Meta:
        model = TenantWorkspace
        fields = ["id", "name", "description", "system_prompt",
                  "created_at", "updated_at", "tenants", "members"]


class CustomWorkspaceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, default="")
    tenant_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1)


class CustomWorkspaceUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantWorkspace
        fields = ["name", "description", "system_prompt"]

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Name cannot be blank.")
        return value
```

**Step 2: Add views to `apps/projects/api/views.py`**

Add after existing views. All imports at module level:

```python
from django.contrib.auth import get_user_model
from django.db.models import Count, Subquery, OuterRef
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.projects.api.custom_workspace_serializers import (
    CustomWorkspaceCreateSerializer,
    CustomWorkspaceDetailSerializer,
    CustomWorkspaceListSerializer,
    CustomWorkspaceUpdateSerializer,
    WorkspaceMemberSerializer,
    WorkspaceTenantSerializer,
)
from apps.projects.models import TenantWorkspace, WorkspaceMembership
from apps.users.models import Tenant, TenantMembership

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
    return role in ROLE_PERMISSIONS.get(action, [])


def _require_role(user, workspace, action):
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=user).first()
    if not membership:
        raise PermissionDenied("Not a member of this workspace.")
    if not _can_perform_action(membership.role, action):
        raise PermissionDenied(f"Requires role: {', '.join(ROLE_PERMISSIONS[action])}")
    return membership


def _validate_tenant_access(user, tenants_qs):
    """Return list of external_ids the user lacks TenantMembership for."""
    workspace_tenant_ids = set(tenants_qs.values_list("external_id", flat=True))
    covered_ids = set(
        TenantMembership.objects.filter(
            user=user, tenant__external_id__in=workspace_tenant_ids
        ).values_list("tenant__external_id", flat=True)
    )
    return list(workspace_tenant_ids - covered_ids)


class CustomWorkspaceListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspaces = (
            TenantWorkspace.objects.filter(
                memberships__user=request.user,
                workspace_type="custom",
            )
            .annotate(
                tenant_count=Count("workspace_tenants", distinct=True),
                member_count=Count("memberships", distinct=True),
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

        tenant_ids = serializer.validated_data["tenant_ids"]
        tenants = Tenant.objects.filter(id__in=tenant_ids)
        if tenants.count() != len(tenant_ids):
            raise ValidationError("One or more tenants not found.")

        missing = _validate_tenant_access(request.user, tenants)
        if missing:
            raise ValidationError(f"No access to tenants: {', '.join(missing)}")

        workspace = TenantWorkspace.objects.create(
            workspace_type="custom",
            name=serializer.validated_data["name"],
            description=serializer.validated_data.get("description", ""),
            created_by=request.user,
        )
        workspace.tenants.set(tenants)
        WorkspaceMembership.objects.create(workspace=workspace, user=request.user, role="owner")

        return Response(
            CustomWorkspaceDetailSerializer(workspace).data,
            status=status.HTTP_201_CREATED,
        )


class CustomWorkspaceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "view")
        return Response(CustomWorkspaceDetailSerializer(workspace).data)

    def patch(self, request, workspace_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "edit_settings")
        serializer = CustomWorkspaceUpdateSerializer(
            workspace, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(CustomWorkspaceDetailSerializer(workspace).data)

    def delete(self, request, workspace_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "delete")
        workspace.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomWorkspaceEnterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        workspace = get_object_or_404(
            TenantWorkspace.objects.prefetch_related(
                "workspace_tenants__tenant", "memberships__user"
            ),
            id=workspace_id,
            workspace_type="custom",
        )
        _require_role(request.user, workspace, "view")

        missing = _validate_tenant_access(request.user, workspace.tenants)
        if missing:
            return Response(
                {"error": "Missing tenant access", "missing_tenants": missing},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(CustomWorkspaceDetailSerializer(workspace).data)


class WorkspaceMemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "view")
        members = workspace.memberships.select_related("user")
        return Response(WorkspaceMemberSerializer(members, many=True).data)

    def post(self, request, workspace_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "manage_members")

        role = request.data.get("role", "viewer")
        if role not in ["editor", "viewer"]:
            raise ValidationError("Role must be 'editor' or 'viewer'.")

        invitee = User.objects.filter(id=request.data.get("user_id")).first()
        if not invitee:
            raise ValidationError("User not found.")

        missing = _validate_tenant_access(invitee, workspace.tenants)
        if missing:
            raise ValidationError(f"Invitee lacks access to tenants: {', '.join(missing)}")

        membership, created = WorkspaceMembership.objects.get_or_create(
            workspace=workspace,
            user=invitee,
            defaults={"role": role, "invited_by": request.user},
        )
        if not created:
            raise ValidationError("User is already a member.")

        return Response(
            WorkspaceMemberSerializer(membership).data,
            status=status.HTTP_201_CREATED,
        )


class WorkspaceMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, workspace_id, member_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "manage_members")

        membership = WorkspaceMembership.objects.filter(
            workspace=workspace, id=member_id
        ).first()
        if not membership:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        role = request.data.get("role")
        if role and role not in ["editor", "viewer"]:
            raise ValidationError("Role must be 'editor' or 'viewer'.")
        if role:
            membership.role = role
            membership.save(update_fields=["role"])

        return Response(WorkspaceMemberSerializer(membership).data)

    def delete(self, request, workspace_id, member_id):
        workspace = get_object_or_404(
            TenantWorkspace, id=workspace_id, workspace_type="custom"
        )
        _require_role(request.user, workspace, "manage_members")

        deleted, _ = WorkspaceMembership.objects.filter(
            workspace=workspace, id=member_id
        ).delete()
        if not deleted:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
```

**Step 3: Create URL file**

`apps/projects/api/custom_workspace_urls.py`:

```python
from django.urls import path

from apps.projects.api.views import (
    CustomWorkspaceDetailView,
    CustomWorkspaceEnterView,
    CustomWorkspaceListCreateView,
    WorkspaceMemberDetailView,
    WorkspaceMemberListCreateView,
)

app_name = "custom_workspaces"

urlpatterns = [
    path("", CustomWorkspaceListCreateView.as_view(), name="list-create"),
    path("<uuid:workspace_id>/", CustomWorkspaceDetailView.as_view(), name="detail"),
    path("<uuid:workspace_id>/enter/", CustomWorkspaceEnterView.as_view(), name="enter"),
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

**Step 4: Register in `config/urls.py`**

Add:
```python
path("api/custom-workspaces/", include("apps.projects.api.custom_workspace_urls")),
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_custom_workspace_api.py -x -v
uv run pytest -x -q
uv run ruff check . && uv run ruff format .
```

**Commit:**

```bash
git add -A && git commit -m "feat: add custom workspace CRUD API with member management and enter endpoint"
```

---

## Phase 5: Agent Context Assembly

### Task 10: Write tests for custom workspace context

**Files:**
- Add to: `tests/test_custom_workspace_api.py` or create `tests/test_custom_workspace_context.py`

```python
from unittest.mock import MagicMock, patch
import pytest


class TestBuildCustomWorkspaceContext:
    """Pure unit tests — no @pytest.mark.django_db. All ORM is mocked."""

    def _make_workspace(self, name="WS", system_prompt="ws prompt"):
        ws = MagicMock()
        ws.name = name
        ws.system_prompt = system_prompt
        return ws

    def _make_tenant_workspace(self, name, system_prompt="", external_id="t1"):
        tw = MagicMock()
        tw.tenant_name = name
        tw.system_prompt = system_prompt
        tw.primary_tenant.return_value.external_id = external_id
        tw.data_dictionary = None
        return tw

    @patch("apps.agents.graph.base.TenantWorkspace")
    @patch("apps.agents.graph.base.AgentLearning")
    @patch("apps.agents.graph.base.KnowledgeEntry")
    def test_workspace_prompt_precedes_tenant_prompts(self, mock_ke, mock_al, mock_tw):
        from apps.agents.graph.base import _build_custom_workspace_context

        workspace = self._make_workspace(system_prompt="workspace prompt")
        tw = self._make_tenant_workspace("Tenant A", system_prompt="tenant prompt")
        mock_tw.objects.filter.return_value = [tw]
        mock_ke.objects.filter.return_value.values.return_value.__getitem__ = lambda s, k: []
        mock_al.objects.filter.return_value.is_active.return_value\
            .order_by.return_value.__getitem__ = lambda s, k: []

        result = _build_custom_workspace_context(workspace)

        prompts = result["system_prompts"]
        assert len(prompts) == 2
        assert "workspace prompt" in prompts[0]
        assert "tenant prompt" in prompts[1]

    @patch("apps.agents.graph.base.TenantWorkspace")
    @patch("apps.agents.graph.base.AgentLearning")
    @patch("apps.agents.graph.base.KnowledgeEntry")
    def test_empty_workspace_prompt_excluded(self, mock_ke, mock_al, mock_tw):
        from apps.agents.graph.base import _build_custom_workspace_context

        workspace = self._make_workspace(system_prompt="")
        mock_tw.objects.filter.return_value = []
        mock_ke.objects.filter.return_value.values.return_value.__getitem__ = lambda s, k: []
        mock_al.objects.filter.return_value.is_active.return_value\
            .order_by.return_value.__getitem__ = lambda s, k: []

        result = _build_custom_workspace_context(workspace)
        assert result["system_prompts"] == []
```

---

### Task 11: Implement custom workspace context builder

**Files:**
- Modify: `apps/agents/graph/base.py`

Add at module level (all imports at top of file):

```python
from apps.knowledge.models import AgentLearning, KnowledgeEntry
from apps.projects.models import TenantWorkspace
```

Add the context builder function:

```python
_CONTEXT_KNOWLEDGE_LIMIT = 200
_CONTEXT_LEARNINGS_LIMIT = 200


def _build_custom_workspace_context(workspace):
    """Build aggregated agent context for a type='custom' TenantWorkspace.

    Aggregates system prompts, knowledge, and learnings from all component
    tenant workspaces plus the custom workspace's own system_prompt.
    Results are capped to avoid unbounded memory usage.
    """
    component_tenant_workspaces = TenantWorkspace.objects.filter(
        workspace_tenants__tenant__in=workspace.tenants.all(),
        workspace_type="tenant",
    )

    # System prompts: workspace-level first, then per-tenant
    prompts = []
    if workspace.system_prompt:
        prompts.append(f"[Workspace: {workspace.name}]\n{workspace.system_prompt}")
    for tw in component_tenant_workspaces:
        if tw.system_prompt:
            prompts.append(f"[Tenant: {tw.name}]\n{tw.system_prompt}")

    knowledge = list(
        KnowledgeEntry.objects.filter(workspace__in=component_tenant_workspaces)
        .values("title", "content", "tags")[:_CONTEXT_KNOWLEDGE_LIMIT]
    )

    learnings = list(
        AgentLearning.objects.filter(
            workspace__in=component_tenant_workspaces,
            is_active=True,
        ).order_by("-confidence_score")[:_CONTEXT_LEARNINGS_LIMIT]
    )

    available_tenants = [
        {
            "tenant_id": tw.tenant_id,
            "tenant_name": tw.name,
            "has_data_dictionary": bool(tw.data_dictionary),
        }
        for tw in component_tenant_workspaces
    ]

    return {
        "system_prompts": prompts,
        "knowledge": knowledge,
        "learnings": learnings,
        "available_tenants": available_tenants,
    }
```

Update the existing agent context initialization to branch on workspace type:

```python
# In the agent graph initialization, after resolving the workspace:
if workspace.workspace_type == "custom":
    context = _build_custom_workspace_context(workspace)
else:
    context = _build_tenant_workspace_context(workspace)  # existing function
```

(Adjust to match the actual function names in `base.py`.)

**Run tests:**

```bash
uv run pytest tests/ -x -q
```

**Commit:**

```bash
git add -A && git commit -m "feat: add _build_custom_workspace_context for multi-tenant agent context assembly"
```

---

## Phase 6: Frontend

### Task 12: Add workspaceSlice to Zustand store

**Files:**
- Create: `frontend/src/store/workspaceSlice.ts`
- Modify: `frontend/src/store/store.ts`

`workspaceSlice.ts`:

```typescript
import { StateCreator } from "zustand"
import { api } from "../api/client"
import type { AppStore } from "./store"

export interface WorkspaceTenant {
  id: string
  external_id: string
  name: string
  added_at: string
}

export interface WorkspaceMember {
  id: string
  user_id: string
  email: string
  role: "owner" | "editor" | "viewer"
  joined_at: string
}

export interface CustomWorkspaceListItem {
  id: string
  name: string
  description: string
  tenant_count: number
  member_count: number
  role: string
  created_at: string
  updated_at: string
}

export interface CustomWorkspaceDetail {
  id: string
  name: string
  description: string
  system_prompt: string
  tenants: WorkspaceTenant[]
  members: WorkspaceMember[]
  created_at: string
  updated_at: string
}

export interface WorkspaceSlice {
  customWorkspaces: CustomWorkspaceListItem[]
  activeCustomWorkspace: CustomWorkspaceDetail | null
  workspaceMode: "tenant" | "custom"
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
      tenant_ids: string[]
    }) => Promise<CustomWorkspaceDetail>
  }
}

export const createWorkspaceSlice: StateCreator<AppStore, [], [], WorkspaceSlice> = (
  set,
  get
) => ({
  customWorkspaces: [],
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
        const data = await api.get<CustomWorkspaceListItem[]>("/api/custom-workspaces/")
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
        activeCustomWorkspace: null,
        workspaceMode: "tenant",
        enterError: null,
        missingTenants: [],
      })
    },
    createCustomWorkspace: async (data) => {
      const created = await api.post<CustomWorkspaceDetail>("/api/custom-workspaces/", data)
      await get().workspaceActions.fetchCustomWorkspaces()
      return created
    },
  },
})
```

Register in `store.ts`:

```typescript
import { WorkspaceSlice, createWorkspaceSlice } from "./workspaceSlice"

export type AppStore = /* existing types */ & WorkspaceSlice

// Add ...createWorkspaceSlice(...a) to the store creator
```

**Lint:**

```bash
cd frontend && bun run lint
```

**Commit:**

```bash
git add -A && git commit -m "feat: add workspaceSlice to Zustand store"
```

---

### Task 13: Create WorkspaceSelector UI

**Files:**
- Create: `frontend/src/components/WorkspaceSelector/WorkspaceSelector.tsx`
- Modify: `frontend/src/components/Sidebar/Sidebar.tsx`

See `docs/plans/2026-03-02-custom-workspace-impl.md` Phase 6 (Task 10) for the full component implementation — the UI design from the original plan is unchanged. The only difference is using `tenant_ids` (not `tenant_workspace_ids`) in the create payload, matching the revised API.

**Lint:**

```bash
cd frontend && bun run lint
```

**Commit:**

```bash
git add -A && git commit -m "feat: add full-width tabbed WorkspaceSelector component"
```

---

## Task Summary

| # | Task | Phase | Depends On |
|---|------|-------|------------|
| 1 | Write model evolution tests | 1 | — |
| 2 | Implement TenantWorkspace M2M + migration | 1 | 1 |
| 3 | Update callsites | 1 | 2 |
| 4 | Write WorkspaceMembership tests | 2 | 3 |
| 5 | Implement WorkspaceMembership + signal + data migration | 2 | 4 |
| 6 | Write WorkspacePermission tests | 3 | 5 |
| 7 | Implement WorkspacePermission | 3 | 6 |
| 8 | Write custom workspace API tests | 4 | 5 |
| 9 | Implement API views/serializers/URLs | 4 | 7, 8 |
| 10 | Write agent context tests | 5 | 3 |
| 11 | Implement `_build_custom_workspace_context` | 5 | 10 |
| 12 | Frontend workspaceSlice | 6 | 9 |
| 13 | WorkspaceSelector UI | 6 | 12 |

Tasks 8–9 (API) and 10–11 (agent context) can be parallelized after Task 5. Tasks 6–7 (permission) can be parallelized with 8–11 once Task 5 is done.

**Note:** Phase 3 (`WorkspacePermission`) is most useful after the URL refactor (`tenant_id` → `workspace_id` in URL paths) lands. Tasks 10–11 can proceed independently of the URL refactor.
