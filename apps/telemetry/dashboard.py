"""Aggregates for the usage dashboard (#862).

Request-path numbers come from ``TelemetryEvent``. Loads, materializations,
recipe runs and created rows also come from the tables that already timestamp
them, so the dashboard has history from before events were recorded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from django.db import connection
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.artifacts.models import Artifact
from apps.chat.models import Thread
from apps.recipes.models import RecipeRunStatus
from apps.telemetry.models import DailySnapshot, EventKind, Outcome, SnapshotMetric
from apps.users.models import Tenant
from apps.workspaces.models import MaterializationRun, Workspace

MAX_DAYS = 365
DEFAULT_DAYS = 30
TOP_N = 20

# Turns the worker runs after a load or for a held request; the person did not act then.
BACKGROUND_TURNS = ["resume", "flush"]
# Kinds that mean a person used the product, for active-user counts.
ACTIVITY_KINDS = [
    EventKind.CHAT_TURN,
    EventKind.ARTIFACT_VIEW,
    EventKind.RECIPE_RUN,
    EventKind.WORKSPACE_SWITCH,
    EventKind.LOGIN,
]
FEATURE_KINDS = {
    "artifact_views": EventKind.ARTIFACT_VIEW,
    "recipe_runs": EventKind.RECIPE_RUN,
    "workspace_switches": EventKind.WORKSPACE_SWITCH,
    "logins": EventKind.LOGIN,
    "turns": EventKind.CHAT_TURN,
}


def _window(days: int, now: datetime) -> tuple[datetime, datetime, list[date]]:
    end_day = now.astimezone(UTC).date()
    start_day = end_day - timedelta(days=days - 1)
    start = datetime.combine(start_day, time.min, tzinfo=UTC)
    return start, now, [start_day + timedelta(days=i) for i in range(days)]


def _rows(sql: str, params: list[Any]) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _ms(value: Any) -> int | None:
    return None if value is None else round(value)


def _series(days: list[date], counts: dict[date, int]) -> list[int]:
    return [counts.get(day, 0) for day in days]


def _daily_counts(queryset, field: str, start: datetime) -> dict[date, int]:
    return {
        row["day"]: row["n"]
        for row in queryset.filter(**{f"{field}__gte": start})
        .annotate(day=TruncDate(field, tzinfo=UTC))
        .values("day")
        .annotate(n=Count("pk"))
    }


def _active_users(start: datetime, now: datetime, days: list[date]) -> dict[str, Any]:
    daily = dict(
        _rows(
            """
            SELECT (occurred_at AT TIME ZONE 'UTC')::date, COUNT(DISTINCT user_id)
            FROM telemetry_telemetryevent
            WHERE kind = ANY(%s) AND occurred_at >= %s AND user_id IS NOT NULL
                AND NOT (kind = %s AND name = ANY(%s))
            GROUP BY 1
            """,
            [ACTIVITY_KINDS, start, EventKind.CHAT_TURN, BACKGROUND_TURNS],
        )
    )

    def distinct_since(since: datetime) -> int:
        [(count,)] = _rows(
            """
            SELECT COUNT(DISTINCT user_id) FROM telemetry_telemetryevent
            WHERE kind = ANY(%s) AND occurred_at >= %s AND user_id IS NOT NULL
                AND NOT (kind = %s AND name = ANY(%s))
            """,
            [ACTIVITY_KINDS, since, EventKind.CHAT_TURN, BACKGROUND_TURNS],
        )
        return count

    return {
        "daily": _series(days, daily),
        "dau": distinct_since(now - timedelta(days=1)),
        "wau": distinct_since(now - timedelta(days=7)),
        "mau": distinct_since(now - timedelta(days=30)),
    }


def _feature_usage(start: datetime, days: list[date]) -> dict[str, list[int]]:
    by_kind: dict[str, dict[date, int]] = {}
    for kind, day, count in _rows(
        """
        SELECT kind, (occurred_at AT TIME ZONE 'UTC')::date, COUNT(*)
        FROM telemetry_telemetryevent
        WHERE kind = ANY(%s) AND occurred_at >= %s AND NOT (kind = %s AND name = ANY(%s))
        GROUP BY 1, 2
        """,
        [list(FEATURE_KINDS.values()), start, EventKind.CHAT_TURN, BACKGROUND_TURNS],
    ):
        by_kind.setdefault(kind, {})[day] = count
    return {label: _series(days, by_kind.get(kind, {})) for label, kind in FEATURE_KINDS.items()}


def _created(start: datetime, days: list[date]) -> dict[str, list[int]]:
    return {
        "threads": _series(days, _daily_counts(Thread.objects.all(), "created_at", start)),
        "artifacts": _series(days, _daily_counts(Artifact.all_objects.all(), "created_at", start)),
        "tenants": _series(days, _daily_counts(Tenant.objects.all(), "created_at", start)),
        "workspaces": _series(days, _daily_counts(Workspace.objects.all(), "created_at", start)),
    }


def _updated(start: datetime, days: list[date]) -> dict[str, list[int | None]]:
    metrics = {
        "threads": SnapshotMetric.THREADS_UPDATED,
        "artifacts": SnapshotMetric.ARTIFACTS_UPDATED,
        "tenants": SnapshotMetric.TENANTS_UPDATED,
        "workspaces": SnapshotMetric.WORKSPACES_UPDATED,
    }
    by_metric: dict[str, dict[date, int]] = {}
    for row in DailySnapshot.objects.filter(
        metric__in=metrics.values(), dimension="", day__gte=start.date()
    ):
        by_metric.setdefault(row.metric, {})[row.day] = row.value
    # A day's counts are written after it ends; a day without a snapshot is unknown, not 0.
    return {
        label: [by_metric.get(metric, {}).get(day) for day in days]
        for label, metric in metrics.items()
    }


def _turns(start: datetime, days: list[date]) -> dict[str, Any]:
    [summary] = _rows(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE outcome = %s),
            COUNT(*) FILTER (WHERE outcome = %s),
            COUNT(*) FILTER (WHERE outcome = %s),
            percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),
            -- A failed turn's first token is its apology text, so only completed
            -- turns say how long a real answer takes to start.
            percentile_cont(0.5) WITHIN GROUP (ORDER BY (attrs->>'ttft_ms')::bigint)
                FILTER (WHERE outcome = %s),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY (attrs->>'ttft_ms')::bigint)
                FILTER (WHERE outcome = %s),
            AVG((attrs->>'tool_calls')::bigint),
            COALESCE(SUM((attrs->>'input_tokens')::bigint), 0),
            COALESCE(SUM((attrs->>'output_tokens')::bigint), 0),
            COALESCE(SUM((attrs->>'cache_read_tokens')::bigint), 0)
        FROM telemetry_telemetryevent
        WHERE kind = %s AND occurred_at >= %s AND name <> ALL(%s)
        """,
        [
            Outcome.COMPLETED,
            Outcome.STOPPED,
            Outcome.FAILED,
            Outcome.COMPLETED,
            Outcome.COMPLETED,
            EventKind.CHAT_TURN,
            start,
            BACKGROUND_TURNS,
        ],
    )
    daily = {
        day: (_ms(p50), _ms(p95), _ms(ttft))
        for day, p50, p95, ttft in _rows(
            """
            SELECT (occurred_at AT TIME ZONE 'UTC')::date,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms),
                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),
                percentile_cont(0.5) WITHIN GROUP (ORDER BY (attrs->>'ttft_ms')::bigint)
                    FILTER (WHERE outcome = %s)
            FROM telemetry_telemetryevent
            WHERE kind = %s AND occurred_at >= %s AND name <> ALL(%s)
            GROUP BY 1
            """,
            [Outcome.COMPLETED, EventKind.CHAT_TURN, start, BACKGROUND_TURNS],
        )
    }
    total, completed, stopped, failed = summary[:4]
    [(background,)] = _rows(
        """
        SELECT COUNT(*) FROM telemetry_telemetryevent
        WHERE kind = %s AND occurred_at >= %s AND name = ANY(%s)
        """,
        [EventKind.CHAT_TURN, start, BACKGROUND_TURNS],
    )
    return {
        "total": total,
        # Resumed and flushed turns the worker ran; not in the numbers above.
        "background": background,
        "outcomes": {"completed": completed, "stopped": stopped, "failed": failed},
        "duration_ms": {"p50": _ms(summary[4]), "p95": _ms(summary[5])},
        "ttft_ms": {"p50": _ms(summary[6]), "p95": _ms(summary[7])},
        "tool_calls_per_turn": round(float(summary[8]), 2) if summary[8] is not None else None,
        "tokens": {
            "input": summary[9],
            "output": summary[10],
            "cache_read": summary[11],
        },
        "daily": [
            {
                "duration_p50_ms": daily.get(day, (None,) * 3)[0],
                "duration_p95_ms": daily.get(day, (None,) * 3)[1],
                "ttft_p50_ms": daily.get(day, (None,) * 3)[2],
            }
            for day in days
        ],
    }


def _tools(start: datetime) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "calls": calls,
            "errors": errors,
            "error_rate": round(errors / calls, 4) if calls else 0,
            "p50_ms": _ms(p50),
            "p95_ms": _ms(p95),
        }
        for name, calls, errors, p50, p95 in _rows(
            """
            SELECT name, COUNT(*), COUNT(*) FILTER (WHERE outcome = %s),
                percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms),
                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
            FROM telemetry_telemetryevent
            WHERE kind = %s AND occurred_at >= %s
            GROUP BY name
            ORDER BY COUNT(*) DESC
            """,
            [Outcome.ERROR, EventKind.TOOL_CALL, start],
        )
    ]


def _tokens_by_workspace(start: datetime) -> list[dict[str, Any]]:
    rows = _rows(
        """
        SELECT workspace_id,
            COALESCE(SUM((attrs->>'input_tokens')::bigint), 0),
            COALESCE(SUM((attrs->>'output_tokens')::bigint), 0),
            COUNT(*)
        FROM telemetry_telemetryevent
        WHERE kind = ANY(%s) AND occurred_at >= %s AND workspace_id IS NOT NULL
        GROUP BY workspace_id
        ORDER BY COALESCE(SUM((attrs->>'input_tokens')::bigint), 0)
            + COALESCE(SUM((attrs->>'output_tokens')::bigint), 0) DESC
        LIMIT %s
        """,
        [[EventKind.CHAT_TURN, EventKind.RECIPE_RUN], start, TOP_N],
    )
    names = dict(
        Workspace.objects.filter(id__in=[row[0] for row in rows]).values_list("id", "name")
    )
    return [
        {
            "workspace_id": str(workspace_id),
            "name": names.get(workspace_id, "(deleted)"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "runs": runs,
        }
        for workspace_id, input_tokens, output_tokens, runs in rows
    ]


def _loads(start: datetime) -> dict[str, Any]:
    """Workspace loads, from the timing table, which predates the events."""
    [(total, failed, p50, p95)] = _rows(
        """
        SELECT COUNT(*), COUNT(*) FILTER (WHERE NOT succeeded),
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM completed_at - started_at) * 1000
            ) FILTER (WHERE succeeded),
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM completed_at - started_at) * 1000
            ) FILTER (WHERE succeeded)
        FROM workspaces_workspaceloadtiming
        WHERE completed_at >= %s
        """,
        [start],
    )
    phases = _rows(
        """
        SELECT phase.key,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY phase.value::text::float * 1000),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY phase.value::text::float * 1000)
        FROM workspaces_workspaceloadtiming,
            jsonb_each(phase_seconds) AS phase
        WHERE completed_at >= %s AND succeeded AND jsonb_typeof(phase.value) = 'number'
        GROUP BY phase.key
        ORDER BY phase.key
        """,
        [start],
    )
    return {
        "total": total,
        "failed": failed,
        "duration_ms": {"p50": _ms(p50), "p95": _ms(p95)},
        "phases": [
            {"phase": phase, "p50_ms": _ms(p50), "p95_ms": _ms(p95)} for phase, p50, p95 in phases
        ],
    }


def _materializations(start: datetime) -> dict[str, Any]:
    states = dict(
        _rows(
            """
            SELECT state, COUNT(*) FROM workspaces_materializationrun
            WHERE started_at >= %s GROUP BY state
            """,
            [start],
        )
    )
    [(p50, p95)] = _rows(
        """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM completed_at - started_at) * 1000
            ),
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM completed_at - started_at) * 1000
            )
        FROM workspaces_materializationrun
        WHERE started_at >= %s AND state = %s AND completed_at IS NOT NULL
        """,
        [start, MaterializationRun.RunState.COMPLETED],
    )
    return {
        "total": sum(states.values()),
        "states": states,
        "duration_ms": {"p50": _ms(p50), "p95": _ms(p95)},
    }


def _recipe_runs(start: datetime) -> dict[str, Any]:
    [(total, failed, p50, p95)] = _rows(
        """
        SELECT COUNT(*), COUNT(*) FILTER (WHERE status = %s),
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM completed_at - started_at) * 1000
            ) FILTER (WHERE status = %s AND started_at IS NOT NULL AND completed_at IS NOT NULL),
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM completed_at - started_at) * 1000
            ) FILTER (WHERE status = %s AND started_at IS NOT NULL AND completed_at IS NOT NULL)
        FROM recipes_reciperun
        WHERE created_at >= %s
        """,
        [
            RecipeRunStatus.FAILED,
            RecipeRunStatus.COMPLETED,
            RecipeRunStatus.COMPLETED,
            start,
        ],
    )
    return {"total": total, "failed": failed, "duration_ms": {"p50": _ms(p50), "p95": _ms(p95)}}


def _schema_sizes(start: datetime, days: list[date]) -> dict[str, Any]:
    total = {
        row.day: row.value
        for row in DailySnapshot.objects.filter(
            metric=SnapshotMetric.SCHEMA_BYTES, dimension="", day__gte=start.date()
        )
    }
    latest_day = (
        DailySnapshot.objects.filter(metric=SnapshotMetric.SCHEMA_BYTES)
        .order_by("-day")
        .values_list("day", flat=True)
        .first()
    )
    top: list[dict[str, Any]] = []
    if latest_day is not None:
        rows = list(
            DailySnapshot.objects.filter(metric=SnapshotMetric.SCHEMA_BYTES, day=latest_day)
            .exclude(dimension="")
            .order_by("-value")[:TOP_N]
        )
        names = {
            str(tenant_id): name
            for tenant_id, name in Tenant.objects.filter(
                id__in=[row.dimension for row in rows]
            ).values_list("id", "canonical_name")
        }
        top = [
            {
                "tenant_id": row.dimension,
                "name": names.get(row.dimension, "(deleted)"),
                "bytes": row.value,
            }
            for row in rows
        ]
    retained = (
        DailySnapshot.objects.filter(
            metric=SnapshotMetric.SCHEMA_BYTES_RETAINED, dimension="", day=latest_day
        )
        .values_list("value", flat=True)
        .first()
        if latest_day is not None
        else None
    )
    return {
        "as_of": latest_day.isoformat() if latest_day else None,
        "retained_bytes": retained,
        "total_daily": [total.get(day) for day in days],
        "top_tenants": top,
    }


def build_dashboard(days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict[str, Any]:
    now = now or timezone.now()
    start, now, day_list = _window(days, now)
    return {
        "window": {"start": start.isoformat(), "end": now.isoformat(), "days": days},
        "days": [day.isoformat() for day in day_list],
        "active_users": _active_users(start, now, day_list),
        "features": _feature_usage(start, day_list),
        "created": _created(start, day_list),
        "updated": _updated(start, day_list),
        "turns": _turns(start, day_list),
        "tools": _tools(start),
        "tokens_by_workspace": _tokens_by_workspace(start),
        "loads": _loads(start),
        "materializations": _materializations(start),
        "recipe_runs": _recipe_runs(start),
        "schema_sizes": _schema_sizes(start, day_list),
    }
