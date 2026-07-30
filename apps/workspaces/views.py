"""
Views for the workspaces app.
"""

import logging

from asgiref.sync import sync_to_async
from django.db import connection
from django.http import JsonResponse
from procrastinate.contrib.django.models import ProcrastinateJob

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
    """
    checks: dict[str, str] = {}
    healthy = True

    for name, probe in (("database", _check_database), ("queue", _check_queue)):
        try:
            await probe()
            checks[name] = "ok"
        except Exception as exc:  # readiness must catch every failure mode
            healthy = False
            checks[name] = "error"
            logger.warning("health_check: %s probe failed: %s", name, exc)

    status = "ok" if healthy else "unhealthy"
    return JsonResponse(
        {"status": status, "checks": checks},
        status=200 if healthy else 503,
    )
