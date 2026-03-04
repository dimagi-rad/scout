import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Thread(models.Model):
    """Indexes chat thread metadata for listing and restoring sessions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_membership = models.ForeignKey(
        "users.TenantMembership",
        on_delete=models.CASCADE,
        related_name="threads",
        null=True,
        blank=True,
    )
    custom_workspace = models.ForeignKey(
        "workspace.CustomWorkspace",
        on_delete=models.CASCADE,
        related_name="threads",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    title = models.CharField(max_length=200, default="New chat")
    is_shared = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["tenant_membership", "user", "-updated_at"],
                name="chat_thread_tm_user_updated",
            ),
        ]
        ordering = ["-updated_at"]

    def clean(self):
        if not self.tenant_membership and not self.custom_workspace:
            raise ValidationError("Either tenant_membership or custom_workspace must be set.")
        if self.tenant_membership and self.custom_workspace:
            raise ValidationError("Only one of tenant_membership or custom_workspace may be set.")

    def save(self, *args, **kwargs):
        if self.is_public and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_public:
            self.share_token = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.id})"
