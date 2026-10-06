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


class WorkspaceMemoryEvent(models.Model):
    """Audit trail of changes to a workspace's shared memory (AgentLearning rows).

    Keeps the text and the actor, not a foreign key to the memory, so the record
    of a deletion outlives the memory it deleted.
    """

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"

    class Source(models.TextChoices):
        CHAT = "chat", "Chat"
        MEMORY_PAGE = "memory_page", "Memory page"

    id = models.BigAutoField(primary_key=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="memory_events"
    )
    memory_id = models.UUIDField(db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    source = models.CharField(max_length=16, choices=Source.choices)
    content = models.TextField(help_text="The memory's text after the change, or as deleted.")
    previous_content = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["workspace", "-created_at"])]

    def __str__(self):
        return f"{self.action} {self.memory_id}"
