import secrets
import uuid

from django.conf import settings
from django.db import models


class Thread(models.Model):
    """Indexes chat thread metadata for listing and restoring sessions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="threads",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    title = models.CharField(max_length=200, default="New chat")
    is_shared = models.BooleanField(default=False)
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["workspace", "user", "-updated_at"],
                name="chat_thread_ws_user_updated",
            ),
        ]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.title} ({self.id})"

    def save(self, *args, **kwargs):
        # Maintain the is_shared ↔ share_token invariant.
        # Always call save() (not update()) to toggle is_shared so this runs.
        if self.is_shared and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_shared:
            self.share_token = None
        super().save(*args, **kwargs)
