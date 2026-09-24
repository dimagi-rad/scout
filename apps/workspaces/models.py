"""
Core models for Scout data agent platform.

Defines Workspace, TenantSchema, and MaterializationRun models.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django_pydantic_field import SchemaField


class SchemaState(models.TextChoices):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    MATERIALIZING = "materializing"
    EXPIRED = "expired"
    TEARDOWN = "teardown"
    FAILED = "failed"


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
    refresh_job_id = models.BigIntegerField(null=True, blank=True, unique=True)
    refresh_workspace_id = models.UUIDField(null=True, blank=True)
    refresh_actor_user_id = models.BigIntegerField(null=True, blank=True)
    refresh_membership_id = models.UUIDField(null=True, blank=True)
    refresh_claimed_at = models.DateTimeField(null=True, blank=True)
    # A workspace-load candidate is PROVISIONING only while its writer holds the
    # tenant lock, so it is reconciled through that lock rather than a queue-job
    # binding (unlike the refresh_* request binding above).
    load_workspace_id = models.UUIDField(null=True, blank=True)
    load_job_id = models.BigIntegerField(null=True, blank=True)
    # Resume evidence: a FAILED candidate is resumed only by a load of the same
    # pending generation whose raw-load configuration still matches.
    load_generation = models.BigIntegerField(null=True, blank=True)
    load_config_fingerprint = models.CharField(max_length=64, blank=True, default="", db_default="")

    class Meta:
        ordering = ["-last_accessed_at"]

    def __str__(self):
        return f"{self.schema_name} ({self.state})"

    def touch(self):
        """Call this on user-initiated actions to reset the inactivity TTL."""
        self.last_accessed_at = timezone.now()
        self.save(update_fields=["last_accessed_at"])

    async def atouch(self):
        """Async version of touch() — reset the inactivity TTL."""
        self.last_accessed_at = timezone.now()
        await self.asave(update_fields=["last_accessed_at"])


class MaterializationRun(models.Model):
    """Records a materialization pipeline execution."""

    class RunState(models.TextChoices):
        STARTED = "started"
        DISCOVERING = "discovering"
        LOADING = "loading"
        TRANSFORMING = "transforming"
        COMPLETED = "completed"
        PARTIAL = "partial"
        FAILED = "failed"
        CANCELLED = "cancelled"
        STALE = "stale"

    ACTIVE_STATES = frozenset(
        {
            "started",
            "discovering",
            "loading",
            "transforming",
        }
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_schema = models.ForeignKey(
        TenantSchema,
        on_delete=models.CASCADE,
        related_name="materialization_runs",
    )
    pipeline = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=RunState.choices, default=RunState.STARTED)
    result = models.JSONField(null=True, blank=True)
    progress = models.JSONField(null=True, blank=True)
    procrastinate_job_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.pipeline} - {self.state}"


class TenantLoadGeneration(models.Model):
    """Bounded per-tenant load-generation ledger for request coalescing.

    ``requested_generation`` advances when a full load is requested while none is
    pending; ``published_generation`` advances when a candidate is promoted. A
    load is pending while ``requested > published``. The published evidence (run,
    schema, fingerprint) is a positive equivalence check for reuse, never a
    fallback to whatever run happens to be latest.
    """

    tenant = models.OneToOneField(
        "users.Tenant",
        on_delete=models.CASCADE,
        related_name="load_generation",
    )
    requested_generation = models.BigIntegerField(default=0)
    published_generation = models.BigIntegerField(default=0)
    published_run = models.ForeignKey(
        MaterializationRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    published_schema = models.ForeignKey(
        TenantSchema,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    published_fingerprint = models.CharField(max_length=64, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return (
            f"generation(tenant={self.tenant_id}, requested={self.requested_generation}, "
            f"published={self.published_generation})"
        )


class WorkspaceDataRecovery(models.Model):
    """Tracks a user-requested repair of a workspace's query surface.

    Materialization runs are tenant-scoped and chat jobs are thread-scoped. An
    artifact recovery is neither: it repairs a workspace-level query surface
    and must remain observable after the requesting page is closed. This row is
    the durable bridge between that UI state and the background work.
    """

    class RecoveryType(models.TextChoices):
        MATERIALIZATION = "materialization", "Materialization"
        VIEW_REBUILD = "view_rebuild", "Workspace view rebuild"
        SEMANTIC_REBUILD = "semantic_rebuild", "Semantic rebuild"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    ACTIVE_STATES = frozenset({State.PENDING, State.RUNNING})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="data_recoveries",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_workspace_data_recoveries",
    )
    recovery_type = models.CharField(max_length=32, choices=RecoveryType.choices)
    source_type = models.CharField(max_length=32, default="artifact")
    source_id = models.UUIDField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    procrastinate_job_id = models.BigIntegerField(null=True, blank=True, unique=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace"],
                condition=Q(state__in=["pending", "running"]),
                name="one_active_data_recovery_per_workspace",
            )
        ]
        indexes = [
            models.Index(fields=["workspace", "state"], name="ws_recovery_ws_state"),
        ]

    def __str__(self):
        return f"{self.recovery_type}({self.state}) for {self.workspace_id}"


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
    # Legacy fields retained from the original per-tenant workspace model
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
    def display_name(self) -> str:
        """Human-facing label: the stored name formatted by the tenant's provider template."""
        t = self.tenant
        if t is None:
            return self.name
        return t.format_display_name(self.name)

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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="workspace_tenants"
    )
    tenant = models.ForeignKey(
        "users.Tenant", on_delete=models.CASCADE, related_name="workspace_tenants"
    )

    class Meta:
        unique_together = [["workspace", "tenant"]]

    def __str__(self):
        return f"{self.workspace} ↔ {self.tenant}"


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


INVITE_TTL_DAYS = 30


def default_invite_expiry():
    return timezone.now() + timedelta(days=INVITE_TTL_DAYS)


class WorkspaceInviteStatus(models.TextChoices):
    PENDING = "pending", "Pending"  # invited; not yet logged into Scout
    AWAITING_ACCESS = "awaiting_access", "Awaiting upstream access"  # logged in, no live tenant
    ACCEPTED = "accepted", "Accepted"  # resolved into a WorkspaceMembership
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


# The two states in which an invite is still awaiting resolution — the ones the
# conditional unique constraint and the resolver operate on.
LIVE_INVITE_STATUSES = (
    WorkspaceInviteStatus.PENDING,
    WorkspaceInviteStatus.AWAITING_ACCESS,
)


class WorkspaceInvite(models.Model):
    """A pre-authorization to join a workspace, keyed by email (not a User row).

    An invite carries NO data access on its own: after Root Cause A,
    apps/workspaces/access.py is the sole authorizer and effective access
    requires a live TenantMembership recomputed every request. The invite only
    auto-resolves into a WorkspaceMembership once the invitee logs into Scout AND
    has live upstream access — so we never create placeholder User rows (the
    blank-account mess A/B cleaned up); the email lives here until they log in.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invites")
    email = models.EmailField()  # stored normalized lower-case
    role = models.CharField(max_length=20, choices=WorkspaceRole.choices)
    status = models.CharField(
        max_length=20,
        choices=WorkspaceInviteStatus.choices,
        default=WorkspaceInviteStatus.PENDING,
    )
    token = models.UUIDField(default=uuid.uuid4, editable=False)  # opaque email deep-link
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(default=default_invite_expiry)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_membership = models.ForeignKey(
        WorkspaceMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=Q(status__in=["pending", "awaiting_access"]),
                name="one_live_invite_per_workspace_email",
            )
        ]

    def __str__(self):
        return f"invite {self.email} -> {self.workspace.name} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return self.expires_at < timezone.now()


# Sentinel prefix in WorkspaceViewSchema.last_error marking a FAILED-by-cascade-
# teardown (vs a genuine build failure). Resume-prompt logic keys off it (arch
# #256, 07#9) to advise re-materializing — which fixes a cascade but not a build
# failure. Embedded in the human-readable message so get_schema_status surfaces it.
VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER = "[cascade-teardown]"
VIEW_SCHEMA_CASCADE_TEARDOWN_ERROR = (
    f"{VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER} A tenant schema this workspace's "
    "combined view depends on was torn down (inactivity TTL or teardown), so the "
    "namespaced views were cascade-dropped. Re-running materialization rebuilds "
    "the tenant data and the view schema."
)


class WorkspaceViewSchema(models.Model):
    """Tracks the PostgreSQL view schema for a multi-tenant workspace.

    For workspaces with 2+ tenants, a physical PostgreSQL schema is created
    containing UNION ALL views that merge the per-tenant schemas.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        Workspace,
        on_delete=models.CASCADE,
        related_name="view_schema",
    )
    schema_name = models.CharField(max_length=255, unique=True)
    state = models.CharField(
        max_length=20,
        choices=SchemaState.choices,
        default=SchemaState.PROVISIONING,
    )
    last_error = models.TextField(
        blank=True,
        default="",
        help_text="Most recent build_view_schema failure message; cleared on a successful build.",
    )
    tenant_coverage = models.JSONField(
        blank=True,
        default=dict,
        help_text=("Tenants included in and excluded from the most recent view-schema build."),
    )
    view_sources = models.JSONField(
        blank=True,
        default=dict,
        # Older processes omit this column during a rolling deployment.
        db_default={},
        help_text="Versioned source identities from the last successful physical view publication.",
    )
    # Managed-side commit marker (COMMENT ON SCHEMA) of the publication this row
    # describes. Control and managed databases are not one transaction; a mismatch
    # means the physical layer committed a build this row never recorded.
    physical_build_token = models.CharField(max_length=32, blank=True, default="", db_default="")
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ViewSchema({self.schema_name}, {self.state})"

    def touch(self):
        """Reset the inactivity TTL for this view schema."""
        self.last_accessed_at = timezone.now()
        self.save(update_fields=["last_accessed_at"])

    async def atouch(self):
        """Async version of touch() — reset the inactivity TTL."""
        self.last_accessed_at = timezone.now()
        await self.asave(update_fields=["last_accessed_at"])


class TenantMetadata(models.Model):
    """Generic provider metadata discovered during the materialize/discover phase.

    Completely provider-agnostic — each provider stores whatever structure it needs
    in the ``metadata`` JSON field. Survives schema teardown so re-provisioning can
    skip re-discovery if the data is still current.

    One row per tenant (#305). It used to hang off ``TenantMembership``, which gave
    a tenant one row per member — rows that could disagree — and cascade-deleted
    metadata the rest of the tenant still needed when a single member left.
    """

    tenant = models.OneToOneField(
        "users.Tenant",
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
        return f"Metadata for {self.tenant}"
