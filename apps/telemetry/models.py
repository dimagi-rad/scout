"""In-app usage and performance telemetry (#862).

Events hold ids, durations, counts, outcomes and tool names only: never message
text, query text or anything a user typed.
"""

from django.db import models
from django.utils import timezone


class EventKind:
    """The event kinds the platform records; ``kind`` is free text so a new one needs no migration."""

    CHAT_TURN = "chat.turn"
    TOOL_CALL = "agent.tool_call"
    WORKSPACE_LOAD = "workspace.load"
    RECIPE_RUN = "recipe.run"
    ARTIFACT_VIEW = "feature.artifact_view"
    WORKSPACE_SWITCH = "feature.workspace_switch"


class Outcome:
    """Runs (turns, loads, recipes) end completed, stopped or failed; single calls ok or error."""

    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    OK = "ok"
    ERROR = "error"


class TelemetryEvent(models.Model):
    """One measured occurrence: a chat turn, a tool call, a load, a feature use."""

    kind = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(default=timezone.now)
    # Plain ids, not foreign keys: history must outlive the rows it describes, and
    # an insert should not take locks on the user or workspace tables.
    user_id = models.BigIntegerField(null=True, blank=True)
    workspace_id = models.UUIDField(null=True, blank=True)
    name = models.CharField(max_length=128, blank=True, default="")
    outcome = models.CharField(max_length=16, blank=True, default="")
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    attrs = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["kind", "occurred_at"], name="telemetry_kind_time"),
            # The retention prune scans by time across every kind.
            models.Index(fields=["occurred_at"], name="telemetry_time"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.name} @ {self.occurred_at:%Y-%m-%d %H:%M:%S}"
