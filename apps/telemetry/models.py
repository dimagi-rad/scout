"""In-app usage and performance telemetry (#862).

Events hold ids, durations, counts, outcomes and tool names only: never message
text, query text or anything a user typed.
"""

from django.db import models
from django.utils import timezone

USAGE_DASHBOARD_PERMISSION = "telemetry.view_usage_dashboard"


class EventKind:
    """The event kinds the platform records; ``kind`` is free text so a new one needs no migration."""

    CHAT_TURN = "chat.turn"
    TOOL_CALL = "agent.tool_call"
    WORKSPACE_LOAD = "workspace.load"
    RECIPE_RUN = "recipe.run"
    ARTIFACT_VIEW = "feature.artifact_view"
    WORKSPACE_SWITCH = "feature.workspace_switch"
    LOGIN = "auth.login"


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
        permissions = [("view_usage_dashboard", "Can view the usage dashboard")]
        indexes = [
            models.Index(fields=["kind", "occurred_at"], name="telemetry_kind_time"),
            # The retention prune scans by time across every kind.
            models.Index(fields=["occurred_at"], name="telemetry_time"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.name} @ {self.occurred_at:%Y-%m-%d %H:%M:%S}"


class SnapshotMetric:
    SCHEMA_BYTES = "schema_bytes"
    # Of the total, bytes in schemas not serving queries: refreshes being built,
    # failed candidates kept for a resume, teardowns still referenced.
    SCHEMA_BYTES_RETAINED = "schema_bytes_retained"
    # Schemas a lock kept from being sized; totals are not written on such a night.
    SCHEMAS_SKIPPED = "schemas_skipped"
    THREADS = "threads"
    ARTIFACTS = "artifacts"
    TENANTS = "tenants"
    WORKSPACES = "workspaces"
    # Enabled accounts, not people who used the product (events say that).
    USERS = "users"
    THREADS_UPDATED = "threads_updated"
    ARTIFACTS_UPDATED = "artifacts_updated"
    WORKSPACES_UPDATED = "workspaces_updated"
    TENANTS_UPDATED = "tenants_updated"


class DailySnapshot(models.Model):
    """One gauge per day, such as a tenant's schema size or the number of threads.

    ``updated_at`` columns keep only the last change, so the day's update counts
    are taken soon after the day ends (a row changed again in those minutes counts
    for the new day only). Rows are tiny and few per day, so they are kept, unlike
    the raw events.
    """

    day = models.DateField()
    metric = models.CharField(max_length=64)
    # A tenant id for per-tenant gauges; empty for platform totals.
    dimension = models.CharField(max_length=64, blank=True, default="")
    value = models.BigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["day", "metric", "dimension"], name="telemetry_snapshot_unique_day"
            )
        ]
        indexes = [models.Index(fields=["metric", "day"], name="telemetry_snapshot_metric_day")]

    def __str__(self) -> str:
        return f"{self.day} {self.metric} {self.dimension} = {self.value}"
