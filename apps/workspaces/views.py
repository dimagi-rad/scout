"""
Views for the workspaces app.
"""

import logging

from asgiref.sync import sync_to_async
from django.db import connection
from django.http import JsonResponse
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.common.capacity import (
    RETRY_AFTER_SECONDS,
    classify_capacity_error,
    report_capacity_exhausted,
)

logger = logging.getLogger(__name__)


async def _check_database() -> None:
    """Probe the platform database with ``SELECT 1``; raises on failure.

    A raw-cursor query is an inherently-sync DB op (not an async-ORM query), so
    ``sync_to_async`` is the correct bridge here.
    """

    @sync_to_async
    def _probe() -> None:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    await _probe()


async def _check_queue() -> None:
    """Probe the Procrastinate (PostgreSQL) queue backend.

    Reading the procrastinate jobs table confirms the queue backend is reachable
    and migrated. Uses the async ORM. Raises on failure.
    """
    await ProcrastinateJob.objects.values_list("id", flat=True).afirst()


async def health_check(request):
    """Readiness check: 200 only when every dependency probe succeeds, else 503
    with a per-check breakdown. A static 200 let a container with a dead DB or
    unreachable queue report healthy — the blind spot in arch #257, finding 08#7.

    A probe refused only for lack of connections reports ``busy`` rather than
    ``unhealthy``: still a 503 for readiness, but the SPA shows a calm "busy"
    notice instead of its "server unreachable" bar.
    """
    checks: dict[str, str] = {}
    capacity_resources = set()

    for name, probe in (("database", _check_database), ("queue", _check_queue)):
        try:
            await probe()
            checks[name] = "ok"
        except Exception as exc:  # readiness must catch every failure mode
            capacity = classify_capacity_error(exc)
            if capacity is not None:
                checks[name] = "busy"
                capacity_resources.add(capacity.resource)
            else:
                checks[name] = "error"
            logger.warning("health_check: %s probe failed: %s", name, exc)

    for resource in capacity_resources:
        await sync_to_async(report_capacity_exhausted)(resource)

    if all(value == "ok" for value in checks.values()):
        return JsonResponse({"status": "ok", "checks": checks})
    status = "busy" if "error" not in checks.values() else "unhealthy"
    response = JsonResponse({"status": status, "checks": checks}, status=503)
    if status == "busy":
        response["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response
