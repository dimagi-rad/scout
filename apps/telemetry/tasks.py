"""Telemetry housekeeping."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.telemetry.models import TelemetryEvent
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
    deleted = 0
    for _ in range(PRUNE_MAX_BATCHES):
        ids = [
            pk
            async for pk in TelemetryEvent.objects.filter(occurred_at__lt=cutoff)
            .order_by("occurred_at")
            .values_list("id", flat=True)[:PRUNE_BATCH_SIZE]
        ]
        if not ids:
            break
        count, _ = await TelemetryEvent.objects.filter(id__in=ids).adelete()
        deleted += count
        if len(ids) < PRUNE_BATCH_SIZE:
            break
    if deleted:
        logger.info("Pruned %d telemetry events older than %s", deleted, cutoff.date())
    return deleted


@app.periodic(cron="23 3 * * *")
@app.task
async def prune_telemetry(timestamp: int = 0) -> dict:
    """Apply the telemetry retention policy once a day."""
    return {"deleted": await prune_telemetry_events()}
