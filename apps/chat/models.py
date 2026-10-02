import uuid

from django.conf import settings
from django.db import models


class Thread(models.Model):
    """Indexes chat thread metadata for listing and restoring sessions."""

    class TitleSource(models.TextChoices):
        FIRST_MESSAGE = "first_message", "First message"
        GENERATED = "generated", "Generated"
        USER = "user", "User"

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
    # The title is generated only while this is FIRST_MESSAGE, so a rename (USER)
    # or an earlier generation is never overwritten; see apps/chat/titles.py.
    title_source = models.CharField(
        max_length=16,
        choices=TitleSource.choices,
        default=TitleSource.FIRST_MESSAGE,
        db_default=TitleSource.FIRST_MESSAGE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    # The agent run currently writing this thread's checkpoint; see apps/chat/turn_lease.py.
    turn_lease_token = models.UUIDField(null=True, blank=True)
    turn_lease_expires_at = models.DateTimeField(null=True, blank=True)

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

    class FailurePhase(models.TextChoices):
        MATERIALIZATION = "materialization", "Materialization"
        QUERY_BUILD = "query_build", "Query layer build"
        RESUME = "resume", "Follow-up response"

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
    failure_phase = models.CharField(
        max_length=20, choices=FailurePhase.choices, blank=True, default="", db_default=""
    )
    # Preflight failures have no MaterializationRun; retain them for resume/recovery.
    materialization_preflight_failures = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["thread", "state"], name="chat_threadjob_th_state"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.job_type}({self.state}) for thread {self.thread_id}"


class PendingRequest(models.Model):
    """What a user typed while their chat's first data load ran: one unsent turn.

    Its text reaches the checkpoint only when the data can answer it, as a single
    HumanMessage; the row is deleted once that message is in the checkpoint. See
    apps/chat/pending_requests.py.
    """

    class State(models.TextChoices):
        WAITING = "waiting", "Waiting"
        # A run holding the thread's turn lease is sending it. A claim whose token is
        # no longer the thread's live lease token is stale: its run ended unsettled.
        CLAIMED = "claimed", "Claimed"

    thread = models.OneToOneField(
        "chat.Thread",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="pending_request",
    )
    # Names the sent message (with the version), so a thread's next request never
    # reuses a message id already in its checkpoint.
    request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    # Each part is {"id", "text", "added_at"}; they are sent joined, in order.
    parts = models.JSONField(default=list)
    # Bumped on every change, so an edit made against an older copy is refused.
    version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=16, choices=State.choices, default=State.WAITING)
    # The load whose resume answers it.
    thread_job = models.ForeignKey(
        "chat.ThreadJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    claim_token = models.UUIDField(null=True, blank=True)
    # Sends tried by the workspace flush, which answers a request no load of its
    # own will resume; capped so one that keeps failing stays the user's to send.
    # db_default too: pods still on the previous release insert rows without it.
    flush_attempts = models.PositiveSmallIntegerField(default=0, db_default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PendingRequest({self.state}, v{self.version}) for thread {self.thread_id}"
