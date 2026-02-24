"""
Core models for Scout data agent platform.

Defines TenantWorkspace, TenantSchema, and MaterializationRun models.
"""

import uuid

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
    tenant_membership = models.ForeignKey(
        "users.TenantMembership",
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
    last_accessed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_accessed_at"]

    def __str__(self):
        return f"{self.schema_name} ({self.state})"


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
    tenant_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Domain name (CommCare) or organization ID. One workspace per tenant.",
    )
    tenant_name = models.CharField(max_length=255)
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
        ordering = ["tenant_name"]

    def __str__(self):
        return f"{self.tenant_name} ({self.tenant_id})"


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
        return f"Metadata for {self.tenant_membership.tenant_id}"
