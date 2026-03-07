"""
Core models for Scout data agent platform.

Defines Workspace, TenantSchema, and MaterializationRun models.
"""

import uuid

from django.conf import settings
from django.db import models
from django_pydantic_field import SchemaField


class SchemaState(models.TextChoices):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    MATERIALIZING = "materializing"
    EXPIRED = "expired"
    TEARDOWN = "teardown"


class TenantSchema(models.Model):
    """Tracks a tenant's provisioned schema in the managed database."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="schemas",
    )
    schema_name = models.CharField(max_length=255, unique=True)
    state = models.CharField(
        max_length=20,
        choices=SchemaState.choices,
        default=SchemaState.PROVISIONING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_accessed_at"]

    def __str__(self):
        return f"{self.schema_name} ({self.state})"

    def touch(self):
        """Call this on user-initiated actions to reset the inactivity TTL."""
        from django.utils import timezone

        self.last_accessed_at = timezone.now()
        self.save(update_fields=["last_accessed_at"])


class MaterializationRun(models.Model):
    """Records a materialization pipeline execution."""

    class RunState(models.TextChoices):
        STARTED = "started"
        DISCOVERING = "discovering"
        LOADING = "loading"
        TRANSFORMING = "transforming"
        COMPLETED = "completed"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_schema = models.ForeignKey(
        TenantSchema,
        on_delete=models.CASCADE,
        related_name="materialization_runs",
    )
    pipeline = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=RunState.choices, default=RunState.STARTED)
    result = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.pipeline} - {self.state}"


class TenantWorkspace(models.Model):
    """Per-tenant workspace holding agent config and serving as FK target for workspace models."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace",
    )
    system_prompt = models.TextField(
        blank=True,
        help_text="Tenant-specific system prompt. Merged with the base agent prompt.",
    )
    data_dictionary = models.JSONField(
        null=True,
        blank=True,
        help_text="Auto-generated schema documentation.",
    )
    data_dictionary_generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__canonical_name"]

    def __str__(self):
        return f"{self.tenant.canonical_name} ({self.tenant.external_id})"

    @property
    def external_tenant_id(self):
        return self.tenant.external_id

    @property
    def tenant_name(self):
        return self.tenant.canonical_name


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
    # Legacy fields carried over from TenantWorkspace for backward compat
    data_dictionary = models.JSONField(null=True, blank=True)
    data_dictionary_generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def tenant(self):
        """Single-tenant compatibility: returns the first associated tenant."""
        return self.tenants.first()

    @property
    def external_tenant_id(self):
        """Compatibility shim: returns the external_id of the first tenant."""
        t = self.tenant
        return t.external_id if t else None

    @property
    def tenant_name(self):
        """Compatibility shim: returns the canonical_name of the first tenant."""
        t = self.tenant
        return t.canonical_name if t else ""


class WorkspaceTenant(models.Model):
    """Junction table linking a Workspace to a Tenant."""

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="workspace_tenants"
    )
    tenant = models.ForeignKey(
        "users.Tenant", on_delete=models.CASCADE, related_name="workspace_tenants"
    )

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


class TenantMetadata(models.Model):
    """Generic provider metadata discovered during the materialize/discover phase.

    Completely provider-agnostic — each provider stores whatever structure it needs
    in the ``metadata`` JSON field. Survives schema teardown so re-provisioning can
    skip re-discovery if the data is still current.
    """

    tenant_membership = models.OneToOneField(
        "users.TenantMembership",
        on_delete=models.CASCADE,
        related_name="metadata",
    )
    # schema=dict is intentionally untyped: the model is provider-agnostic and
    # each loader defines its own structure. A typed Pydantic schema can be
    # introduced per-provider without a migration when the need arises.
    metadata: dict = SchemaField(
        schema=dict,
        default=dict,
        help_text="Provider-specific metadata blob. Structure defined by the loader.",
    )
    discovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this metadata was last successfully fetched from the provider",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tenant Metadata"
        verbose_name_plural = "Tenant Metadata"

    def __str__(self) -> str:
        return f"Metadata for {self.tenant_membership.tenant}"
