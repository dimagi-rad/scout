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
        # Maintain the is_shared ↔ share_token invariant.
        # Always call save() (not update()) to toggle is_shared so this runs.
        if self.is_shared and not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
        elif not self.is_shared:
            self.share_token = None
        super().save(*args, **kwargs)


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
    # Staleness for an in-flight resume is measured from this RESUME-phase
    # timestamp, NOT created_at: created_at includes the full materialization +
    # queue time, so a healthy long-running materialization (>10 min) followed
    # by a freshly-started resume would otherwise be falsely flipped to FAILED
    # by the reconciler while the resume is still live. Null until a resume
    # starts (so a never-claimed PENDING job still ages out from created_at).
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Human-readable failure summary populated on FAILED/CANCELLED transitions
    # so the frontend can render an inline error card after the spinner clears.
    # Composed from MaterializationRun.result["sources"] when available, or a
    # generic string when the failure has no per-source detail (e.g. agent
    # ainvoke crash, janitor stuck-job flip).
    error_summary = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["thread", "state"], name="chat_threadjob_th_state"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.job_type}({self.state}) for thread {self.thread_id}"
