"""Daily gauges: tenant schema sizes and platform totals from existing tables (#862)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import psycopg
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from apps.artifacts.models import Artifact
from apps.chat.models import Thread
from apps.telemetry.models import DailySnapshot, SnapshotMetric
from apps.users.models import Tenant, User
from apps.workspaces.models import SchemaState, TenantSchema, Workspace
from apps.workspaces.services.schema_manager import get_managed_db_connection

logger = logging.getLogger(__name__)

# Only EXPIRED is set after the drop. FAILED candidates kept for a resume and
# TEARDOWN schemas a reference still holds are on disk too, and worth seeing.
_GONE_STATES = [SchemaState.EXPIRED]
# A load holds ACCESS EXCLUSIVE on a table it is replacing; skip that schema for
# the night rather than wait on it.
_SIZE_LOCK_TIMEOUT_MS = 2_000
_SIZE_STATEMENT_TIMEOUT_MS = 30_000

_SCHEMA_SIZE_SQL = """
    SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint, COUNT(*)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relkind IN ('r', 'm', 'p')
"""


@sync_to_async(thread_sensitive=False)
def _schema_sizes(schema_names: list[str]) -> tuple[dict[str, int], list[str]]:
    """Bytes on disk per schema (tables with their indexes and TOAST), and the skipped.

    One short-lived connection a day, through the same helper every load uses;
    the managed database is a different database, so Django's connection cannot
    see its catalogs. Each schema is its own autocommit statement, so one that
    is locked or slow is skipped without losing the rest.
    """
    sizes: dict[str, int] = {}
    skipped: list[str] = []
    if not schema_names:
        return sizes, skipped
    with get_managed_db_connection() as conn:
        conn.execute(f"SET lock_timeout = {_SIZE_LOCK_TIMEOUT_MS}")
        conn.execute(f"SET statement_timeout = {_SIZE_STATEMENT_TIMEOUT_MS}")
        for name in schema_names:
            try:
                size, relations = conn.execute(_SCHEMA_SIZE_SQL, [name]).fetchone()
            except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
                skipped.append(name)
                continue
            if relations:
                sizes[name] = int(size)
    if skipped:
        logger.warning("Skipped sizing %d locked or slow schema(s): %s", len(skipped), skipped)
    return sizes, skipped


async def _schema_size_rows(day: date) -> list[DailySnapshot]:
    schemas: dict[str, tuple[str, str]] = {
        name: (str(tenant_id), state)
        async for name, tenant_id, state in TenantSchema.objects.exclude(
            state__in=_GONE_STATES
        ).values_list("schema_name", "tenant_id", "state")
    }
    try:
        sizes, skipped = await _schema_sizes(sorted(schemas))
    except Exception:
        logger.warning("Could not measure tenant schema sizes", exc_info=True)
        return []
    # A partial number would read as a drop in storage; better no number that night.
    incomplete = {schemas[name][0] for name in skipped}
    per_tenant: dict[str, int] = {}
    retained = 0
    for schema_name, size in sizes.items():
        tenant_id, state = schemas[schema_name]
        per_tenant[tenant_id] = per_tenant.get(tenant_id, 0) + size
        if state != SchemaState.ACTIVE:
            retained += size
    rows = [
        DailySnapshot(day=day, metric=SnapshotMetric.SCHEMA_BYTES, dimension=tenant_id, value=size)
        for tenant_id, size in per_tenant.items()
        if tenant_id not in incomplete
    ]
    rows.append(DailySnapshot(day=day, metric=SnapshotMetric.SCHEMAS_SKIPPED, value=len(skipped)))
    if not skipped:
        rows.append(
            DailySnapshot(
                day=day, metric=SnapshotMetric.SCHEMA_BYTES, value=sum(per_tenant.values())
            )
        )
        rows.append(
            DailySnapshot(day=day, metric=SnapshotMetric.SCHEMA_BYTES_RETAINED, value=retained)
        )
    return rows


GAUGE_METRICS = frozenset(
    {
        SnapshotMetric.THREADS,
        SnapshotMetric.ARTIFACTS,
        SnapshotMetric.TENANTS,
        SnapshotMetric.WORKSPACES,
        SnapshotMetric.USERS,
    }
)


async def _count_rows(day: date, *, gauges: bool) -> list[DailySnapshot]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    updated = {"updated_at__gte": start, "updated_at__lt": end}
    counts = {
        SnapshotMetric.THREADS: Thread.objects.all(),
        SnapshotMetric.ARTIFACTS: Artifact.objects.all(),
        SnapshotMetric.TENANTS: Tenant.objects.all(),
        SnapshotMetric.WORKSPACES: Workspace.objects.all(),
        SnapshotMetric.USERS: User.objects.filter(is_active=True),
        SnapshotMetric.THREADS_UPDATED: Thread.objects.filter(**updated),
        # all_objects: a soft delete that day is a change too.
        SnapshotMetric.ARTIFACTS_UPDATED: Artifact.all_objects.filter(**updated),
        SnapshotMetric.TENANTS_UPDATED: Tenant.objects.filter(**updated),
        SnapshotMetric.WORKSPACES_UPDATED: Workspace.objects.filter(**updated),
    }
    return [
        DailySnapshot(day=day, metric=metric, value=await queryset.acount())
        for metric, queryset in counts.items()
        if gauges or metric not in GAUGE_METRICS
    ]


async def take_daily_snapshot(day: date | None = None, *, gauges: bool | None = None) -> int:
    """Record ``day``'s metrics (yesterday by default); rerunning a day replaces them.

    Totals and schema sizes are read as they are now, so they are written only
    for the day just ended unless ``gauges`` says otherwise; a backfill of an
    older day recomputes its update counts alone instead of overwriting history.
    """
    yesterday = (timezone.now() - timedelta(days=1)).date()
    day = day or yesterday
    if gauges is None:
        gauges = day == yesterday
    rows = await _count_rows(day, gauges=gauges)
    if gauges:
        rows += await _schema_size_rows(day)
    await _replace_day(day, rows, sizes=gauges)
    return len(rows)


@sync_to_async
def _replace_day(day: date, rows: list[DailySnapshot], *, sizes: bool) -> None:
    with transaction.atomic():
        if sizes:
            # Tenants measured on an earlier run of this day may be gone or skipped now.
            DailySnapshot.objects.filter(day=day, metric=SnapshotMetric.SCHEMA_BYTES).exclude(
                dimension=""
            ).delete()
        DailySnapshot.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["day", "metric", "dimension"],
            update_fields=["value"],
        )
