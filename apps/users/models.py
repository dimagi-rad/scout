"""
Custom User model for Scout data agent platform.

Extends Django's AbstractUser with additional fields for the platform.
"""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """Custom manager for User model with email as the unique identifier."""

    def create_user(self, email=None, password=None, **extra_fields):
        """Create and save a regular user with the given email and password."""
        # store NULL, not empty string, to avoid unique constraint collisions
        email = self.normalize_email(email) if email else None
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    async def acreate_user(self, email=None, password=None, **extra_fields):
        """Async variant of create_user.

        Django's own ``acreate_user`` lives on ``UserManager``; this manager
        subclasses ``BaseUserManager`` so it must provide its own, mirroring
        create_user (password hashing is CPU-only, no DB access).
        """
        email = self.normalize_email(email) if email else None
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        await user.asave(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create and save a superuser with the given email and password."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Custom User model for the Scout platform.

    Uses email as the primary identifier for authentication.
    """

    email = models.EmailField(unique=True, blank=True, null=True)

    # Override username to make it optional
    username = models.CharField(max_length=150, blank=True)

    avatar_url = models.URLField(blank=True)
    timezone = models.CharField(max_length=50, default="UTC")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ["email"]

    def save(self, *args, **kwargs):
        if not self.email:
            self.email = None
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email or self.username or f"user-{self.pk}"

    def get_full_name(self):
        """Return the first_name plus the last_name, with a space in between."""
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.email or self.username or ""


PROVIDER_CHOICES = [
    ("commcare", "CommCare HQ"),
    ("commcare_connect", "CommCare Connect"),
    ("ocs", "Open Chat Studio"),
]

# Per-provider templates applied to a workspace's stored name to produce a display name.
# Fields available: {name} (workspace name), plus any field on the source Tenant
# (e.g. {canonical_name}, {external_id}, {provider}).
PROVIDER_DISPLAY_TEMPLATES: dict[str, str] = {
    "commcare": "{name}",
    "commcare_connect": "{name} (Opp {external_id})",
    "ocs": "{name} (Bot {external_id})",
}


class Tenant(models.Model):
    """Canonical tenant identity record, created only after provider verification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    external_id = models.CharField(
        max_length=255,
        help_text="Provider-assigned identifier (CommCare domain name or Connect org ID).",
    )
    canonical_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["provider", "external_id"]]
        ordering = ["canonical_name"]

    def __str__(self):
        return f"{self.provider}:{self.external_id} ({self.canonical_name})"

    def format_display_name(self, workspace_name: str) -> str:
        """Apply this tenant's provider template to ``workspace_name``.

        Falls back to ``workspace_name`` unchanged if the provider has no
        template or the template references a field we can't resolve.
        """
        template = PROVIDER_DISPLAY_TEMPLATES.get(self.provider)
        if not template:
            return workspace_name
        try:
            return template.format(
                name=workspace_name,
                canonical_name=self.canonical_name,
                external_id=self.external_id,
                provider=self.provider,
            )
        except (KeyError, IndexError):
            return workspace_name


class TenantConnection(models.Model):
    """A single credential a user added: one OAuth login or one API key.

    A connection is a credential only. The team a chatbot belongs to is recorded
    on TenantMembership (provider_metadata): in v1 a user has at most one OAuth
    connection per provider, and its team can change when they re-authorize.
    """

    OAUTH = "oauth"
    API_KEY = "api_key"
    TYPE_CHOICES = [
        (OAUTH, "OAuth Token"),
        (API_KEY, "API Key"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_connections",
    )
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    credential_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    encrypted_credential = models.CharField(
        max_length=2000,
        blank=True,
        help_text="Fernet-encrypted opaque string. Empty for OAuth (token lives in allauth).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"],
                condition=models.Q(credential_type="oauth"),
                name="unique_oauth_connection_per_user_provider",
            ),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.provider}:{self.credential_type}"


class LiveTenantMembershipManager(models.Manager):
    """Default manager: hides archived (upstream-revoked) memberships.

    An archived TenantMembership is a *tombstone* for access that Connect/HQ/OCS
    revoked — not a soft-deleted record a human can restore. Every access read must
    therefore be blind to it by default, so this manager filters ``archived_at``
    out. The unfiltered ``all_objects`` manager is the deliberate escape hatch for
    the few places that need tombstones: the resolver's un-archive-on-re-grant step,
    the merge service, admin, and connection management. ``Meta.base_manager_name``
    points at ``all_objects`` so cascade deletion still collects archived rows;
    reverse managers (``conn.memberships`` etc.) inherit THIS class, so any of them
    that must see tombstones use ``all_objects`` explicitly.
    """

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


class TenantMembership(models.Model):
    """Links a user to a verified Tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    connection = models.ForeignKey(
        "TenantConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
    )
    provider_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider-specific data (e.g. OCS team_slug/team_name); empty for providers without it.",
    )
    last_selected_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # ``objects`` (default) is live-only so access reads can never see a revoked
    # tombstone. ``all_objects`` is the escape hatch for writes/merges/resolution.
    objects = LiveTenantMembershipManager()
    # ruff reads the team_slug/team_name @property accessors below as "fields" and
    # wants them before this manager; they legitimately live after Meta.
    all_objects = models.Manager()  # noqa: DJ012

    class Meta:
        unique_together = [["user", "tenant"]]
        ordering = ["-last_selected_at", "tenant__canonical_name"]
        base_manager_name = "all_objects"

    def __str__(self):
        return f"TenantMembership({self.user_id} - {self.tenant_id})"

    # ``team_slug``/``team_name`` are OCS-specific, so they live in
    # ``provider_metadata`` rather than as columns on this generic model. These
    # accessors keep the keys in one place (the OAuth fail-closed check reads
    # ``team_slug``) and let callers and ``objects.create(team_slug=...)`` use
    # them as if they were fields.
    @property
    def team_slug(self) -> str:
        return (self.provider_metadata or {}).get("team_slug", "")

    @team_slug.setter
    def team_slug(self, value: str) -> None:
        self.provider_metadata = {**(self.provider_metadata or {}), "team_slug": value}

    @property
    def team_name(self) -> str:
        return (self.provider_metadata or {}).get("team_name", "")

    @team_name.setter
    def team_name(self, value: str) -> None:
        self.provider_metadata = {**(self.provider_metadata or {}), "team_name": value}
