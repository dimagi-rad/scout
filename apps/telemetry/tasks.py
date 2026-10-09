"""Telemetry housekeeping."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from django.db.models import Subquery
from django.utils import timezone

from apps.telemetry.models import TelemetryEvent
from apps.telemetry.snapshots import take_daily_snapshot
from config.procrastinate import app

logger = logging.getLogger(__name__)

# A year plus a month: enough to compare a month with the same month last year.
RETENTION_DAYS = 400
PRUNE_BATCH_SIZE = 5_000
# Bounds one run; a backlog larger than this drains over the following nights.
PRUNE_MAX_BATCHES = 200


async def prune_telemetry_events(now=None) -> int:
    """Delete events older than the retention window in small batches; returns the count."""
    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    expired = TelemetryEvent.objects.filter(occurred_at__lt=cutoff).order_by("occurred_at")
    deleted = 0
    for _ in range(PRUNE_MAX_BATCHES):
        batch = expired.values("id")[:PRUNE_BATCH_SIZE]
        count, _ = await TelemetryEvent.objects.filter(id__in=Subquery(batch)).adelete()
        deleted += count
        if count < PRUNE_BATCH_SIZE:
            break
    else:
        if await expired.aexists():
            logger.warning("Telemetry prune hit its batch budget; the rest waits for the next run")
    if deleted:
        logger.info("Pruned %d telemetry events older than %s", deleted, cutoff.date())
    return deleted


@app.periodic(cron="23 3 * * *")
@app.task
async def prune_telemetry(timestamp: int = 0) -> dict:
    """Apply the telemetry retention policy once a day."""
    return {"deleted": await prune_telemetry_events()}


@app.periodic(cron="20 0 * * *")
@app.task
async def snapshot_daily_metrics(timestamp: int = 0) -> dict:
    """Record the day before the scheduled run: schema sizes, totals, update counts.

    The day comes from the schedule, so a run delayed past midnight by a busy
    worker still records the right one.
    """
    now = timezone.now()
    scheduled = datetime.fromtimestamp(timestamp, UTC) if timestamp else now
    day = (scheduled - timedelta(days=1)).date()
    # Totals and sizes are read as of now; a run stuck in the queue past a day
    # would stamp today's values on an older day.
    gauges = now - scheduled < timedelta(hours=20)
    return {"rows": await take_daily_snapshot(day, gauges=gauges)}
