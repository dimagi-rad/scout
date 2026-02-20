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

    def create_user(self, email, password=None, **extra_fields):
        """Create and save a regular user with the given email and password."""
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
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

    email = models.EmailField(unique=True)

    # Override username to make it optional
    username = models.CharField(max_length=150, blank=True)

    # Additional profile fields
    avatar_url = models.URLField(blank=True)
    timezone = models.CharField(max_length=50, default="UTC")

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.email

    def get_full_name(self):
        """Return the first_name plus the last_name, with a space in between."""
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.email


class TenantMembership(models.Model):
    """Links users to tenants discovered from OAuth providers."""

    PROVIDER_CHOICES = [
        ("commcare", "CommCare HQ"),
        ("commcare_connect", "CommCare Connect"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    tenant_id = models.CharField(
        max_length=255,
        help_text="Domain name (CommCare) or organization ID (Connect)",
    )
    tenant_name = models.CharField(
        max_length=255,
        help_text="Human-readable tenant name",
    )
    last_selected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "provider", "tenant_id"]
        ordering = ["-last_selected_at", "tenant_name"]

    def __str__(self):
        return f"{self.user.email} - {self.provider}:{self.tenant_id}"


class TenantCredential(models.Model):
    """Stores credentials for a tenant — either OAuth pointer or encrypted API key.

    For credential_type == OAUTH: encrypted_credential is blank; the actual
    token lives in allauth's SocialToken and is retrieved from there.

    For credential_type == API_KEY: encrypted_credential holds a Fernet-encrypted
    opaque string. Format is provider-specific, e.g. "username:apikey" for CommCare.
    """

    OAUTH = "oauth"
    API_KEY = "api_key"
    TYPE_CHOICES = [
        (OAUTH, "OAuth Token"),
        (API_KEY, "API Key"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_membership = models.OneToOneField(
        TenantMembership,
        on_delete=models.CASCADE,
        related_name="credential",
    )
    credential_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    encrypted_credential = models.CharField(
        max_length=2000,
        blank=True,
        help_text="Fernet-encrypted opaque string. Empty for OAuth type.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant_membership} ({self.credential_type})"
