"""Daily gauges: tenant schema sizes and platform totals from existing tables (#862)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import psycopg
from asgiref.sync import sync_to_async
from django.db import transaction

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
        conn.execute("SET lock_timeout = 2000")
        conn.execute("SET statement_timeout = 30000")
        for name in schema_names:
            # A load holds ACCESS EXCLUSIVE on a table it is replacing; the 2s lock
            # timeout skips that schema for the night rather than wait on it.
            try:
                size, relations = conn.execute(_SCHEMA_SIZE_SQL, [name]).fetchone() or (0, 0)
            except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
                skipped.append(name)
                continue
            if relations:
                sizes[name] = int(size)
    if skipped:
        logger.warning("Skipped sizing %d locked or slow schema(s): %s", len(skipped), skipped)
    return sizes, skipped


async def _schema_size_rows(day: date) -> tuple[list[DailySnapshot], bool]:
    """The night's size rows, and whether the sizing ran at all."""
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
        # Every schema went unmeasured; say so rather than look like a clean night.
        skipped_all = DailySnapshot(
            day=day, metric=SnapshotMetric.SCHEMAS_SKIPPED, value=len(schemas)
        )
        return [skipped_all], False
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
    return rows, True


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


async def take_daily_snapshot(day: date, *, gauges: bool = False) -> int:
    """Record ``day``'s metrics; rerunning a day replaces them.

    Totals and schema sizes are read as they are now, so only the caller that
    knows ``day`` just ended (the nightly task) passes ``gauges``; otherwise only
    the day's change counts are recomputed and history is left alone.
    """
    rows = await _count_rows(day, gauges=gauges)
    measured = False
    if gauges:
        size_rows, measured = await _schema_size_rows(day)
        rows += size_rows
    # A failed sizing keeps an earlier run's sizes for the day rather than erase them.
    await _replace_day(day, rows, sizes=measured)
    return len(rows)


_SIZE_METRICS = [SnapshotMetric.SCHEMA_BYTES, SnapshotMetric.SCHEMA_BYTES_RETAINED]


@sync_to_async
def _replace_day(day: date, rows: list[DailySnapshot], *, sizes: bool) -> None:
    with transaction.atomic():
        if sizes:
            # A rerun's sizes replace the earlier run's whole set: a tenant gone or
            # skipped now, or totals withheld now, must not keep its old value.
            DailySnapshot.objects.filter(day=day, metric__in=_SIZE_METRICS).delete()
        DailySnapshot.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["day", "metric", "dimension"],
            update_fields=["value"],
        )
