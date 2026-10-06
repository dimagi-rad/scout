"""Memories the agent carries between chat sessions (#849)."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


class PersonalMemory(models.Model):
    """A user's own preference or habit, applied in every workspace they chat in.

    Private to its owner: nothing outside the owner's chats and their Memory page
    reads it, workspace managers included.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_memories",
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["user", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                "user", Lower("content"), name="unique_personal_memory_per_user"
            )
        ]

    def __str__(self):
        return self.content[:50]
