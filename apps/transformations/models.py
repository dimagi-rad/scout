import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models


class TransformationScope(models.TextChoices):
    SYSTEM = "system"
    TENANT = "tenant"
    WORKSPACE = "workspace"


class TransformationAsset(models.Model):
    """A dbt model definition stored as a first-class asset.

    Each asset corresponds to one .sql file that dbt will execute,
    producing one table or view.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        validators=[
            RegexValidator(
                regex=r"^[a-z][a-z0-9_]*$",
                message="Name must be a valid dbt model name: lowercase letters, digits, and underscores, starting with a letter.",
            ),
        ],
        help_text="dbt model name. Must be unique within scope+container.",
    )
    description = models.TextField(blank=True)
    scope = models.CharField(max_length=20, choices=TransformationScope.choices)
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="transformation_assets",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="transformation_assets",
    )
    sql_content = models.TextField(
        help_text="dbt model SQL. Uses ref() within scope, direct table names across scopes.",
    )
    replaces = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replaced_by",
        help_text="The TransformationAsset this model supersedes for querying.",
    )
    test_yaml = models.TextField(
        blank=True,
        help_text="dbt schema test YAML for this model.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(tenant__isnull=False, workspace__isnull=True)
                    | models.Q(tenant__isnull=True, workspace__isnull=False)
                ),
                name="transformation_asset_one_container",
            ),
            models.UniqueConstraint(
                fields=["name", "scope", "tenant"],
                condition=models.Q(tenant__isnull=False),
                name="unique_asset_name_per_tenant_scope",
            ),
            models.UniqueConstraint(
                fields=["name", "scope", "workspace"],
                condition=models.Q(workspace__isnull=False),
                name="unique_asset_name_per_workspace_scope",
            ),
        ]
        ordering = ["scope", "name"]

    def __str__(self):
        container = self.tenant or self.workspace
        return f"{self.scope}:{self.name} ({container})"

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        if self.scope in (TransformationScope.SYSTEM, TransformationScope.TENANT):
            if not self.tenant_id:
                raise ValidationError("System and tenant scoped assets require a tenant.")
            if self.workspace_id:
                raise ValidationError("System and tenant scoped assets must not have a workspace.")
        elif self.scope == TransformationScope.WORKSPACE:
            if not self.workspace_id:
                raise ValidationError("Workspace scoped assets require a workspace.")
            if self.tenant_id:
                raise ValidationError("Workspace scoped assets must not have a tenant.")


class TransformationRunStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TransformationRun(models.Model):
    """Pipeline-level execution record for a full transformation cycle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="transformation_runs",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="transformation_runs",
    )
    status = models.CharField(
        max_length=20,
        choices=TransformationRunStatus.choices,
        default=TransformationRunStatus.PENDING,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"TransformationRun({self.tenant}, {self.status})"


class AssetRunStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TransformationAssetRun(models.Model):
    """Per-model execution record within a pipeline run."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        TransformationRun,
        on_delete=models.CASCADE,
        related_name="asset_runs",
    )
    asset = models.ForeignKey(
        TransformationAsset,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    status = models.CharField(
        max_length=20,
        choices=AssetRunStatus.choices,
        default=AssetRunStatus.PENDING,
    )
    duration_ms = models.IntegerField(null=True, blank=True)
    logs = models.TextField(blank=True)
    test_results = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at"]

    def __str__(self):
        return f"AssetRun({self.asset.name}, {self.status})"
