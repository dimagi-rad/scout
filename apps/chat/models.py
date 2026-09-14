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
    title = models.CharField(max_length=203, default="", blank=True)
    title_is_custom = models.BooleanField(default=False)
    is_shared = models.BooleanField(default=False)
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

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
        # Maintain the is_shared ↔ share_token invariant — toggle via save(), not update().
        if self.is_shared and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_shared:
            self.share_token = None
        super().save(*args, **kwargs)


class ThreadArtifact(models.Model):
    """Explicit file relationship between a chat thread and an artifact version."""

    class Source(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        MENTIONED = "mentioned", "Mentioned"
        ATTACHED = "attached", "Attached"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        "chat.Thread",
        on_delete=models.CASCADE,
        related_name="artifact_links",
    )
    artifact = models.ForeignKey(
        "artifacts.Artifact",
        on_delete=models.CASCADE,
        related_name="thread_links",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="thread_artifact_links",
    )
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MENTIONED)
    message_id = models.CharField(max_length=128, blank=True, default="")
    tool_call_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "artifact"],
                name="unique_thread_artifact_link",
            )
        ]
        indexes = [
            models.Index(fields=["thread", "-last_seen_at"], name="chat_thart_thread_seen"),
            models.Index(fields=["workspace", "-last_seen_at"], name="chat_thart_ws_seen"),
        ]
        ordering = ["-last_seen_at"]

    def __str__(self):
        return f"{self.artifact_id} in thread {self.thread_id}"


class ThreadJob(models.Model):
    """Tracks a long-running background job (materialization, etc.) tied to a chat thread.

    The frontend polls active jobs to drive sidebar indicators and live progress;
    the resume worker uses ``tool_call_id`` to inject completion into the
    LangGraph conversation when the job finishes.
    """

    class JobType(models.TextChoices):
        MATERIALIZATION = "materialization", "Materialization"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL_STATES = frozenset({State.COMPLETED, State.FAILED, State.CANCELLED})
    ACTIVE_STATES = frozenset({State.PENDING, State.RUNNING})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey("chat.Thread", on_delete=models.CASCADE, related_name="jobs")
    job_type = models.CharField(max_length=32, choices=JobType.choices)
    procrastinate_job_id = models.BigIntegerField(unique=True, db_index=True)
    tool_call_id = models.CharField(max_length=64)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set when the resume task claims the job (PENDING/CANCELLED -> RUNNING).
    # In-flight resume staleness is measured from here, NOT created_at: created_at
    # includes materialization + queue time, so a healthy long materialization
    # followed by a fresh resume would otherwise be falsely flipped to FAILED.
    # Null until a resume starts (never-claimed PENDING jobs age out from created_at).
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Failure summary for the frontend error card, populated on FAILED/CANCELLED
    # from MaterializationRun.result["sources"] when available, else a generic string.
    error_summary = models.TextField(blank=True, default="")
    # Preflight failures have no MaterializationRun; retain them for resume/recovery.
    materialization_preflight_failures = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["thread", "state"], name="chat_threadjob_th_state"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.job_type}({self.state}) for thread {self.thread_id}"
