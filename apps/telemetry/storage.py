"""Storage headroom: host disk free space and platform database size.

In September 2026 the host's root disk filled with retained Docker images and
every deploy failed for six days before anyone noticed (DEPLOYMENT.md, "Host
disk full"). The CloudWatch alarms that cover RDS have no subscribers, and
nothing watched the host disk at all, so this check reports through Sentry,
which already pages ``#scout-ops``.

The worker's ``/`` is an overlay2 mount. overlayfs answers ``statfs`` from its
upper layer, which lives under Docker's data root on the host, so
``shutil.disk_usage("/")`` reports the filesystem that image pulls fill — the
one ``scripts/host-disk-guard.sh`` checks before each deploy.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

import sentry_sdk
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

WARNING = "warning"
CRITICAL = "critical"

_LARGEST_TABLES = 5

_LARGEST_TABLES_SQL = """
    SELECT n.nspname || '.' || c.relname, pg_total_relation_size(c.oid)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'm', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%%'
    ORDER BY 2 DESC
    LIMIT %s
"""


@dataclass(frozen=True)
class DiskReading:
    path: str
    total_bytes: int
    free_bytes: int

    @property
    def free_percent(self) -> float:
        return 100.0 * self.free_bytes / self.total_bytes if self.total_bytes else 0.0


def read_disk(path: str) -> DiskReading:
    usage = shutil.disk_usage(path)
    return DiskReading(path=path, total_bytes=usage.total, free_bytes=usage.free)


def disk_severity(reading: DiskReading) -> str | None:
    # The absolute floor is the deploy guard's limit: below it deploys are refused,
    # whatever percentage that is on a resized volume.
    if (
        reading.free_percent < settings.STORAGE_DISK_CRITICAL_FREE_PERCENT
        or reading.free_bytes < settings.STORAGE_DISK_CRITICAL_FREE_GIB * 2**30
    ):
        return CRITICAL
    if reading.free_percent < settings.STORAGE_DISK_WARNING_FREE_PERCENT:
        return WARNING
    return None


def _size(n: int) -> str:
    if n < 2**30:
        return f"{n / 2**20:.0f} MiB"
    return f"{n / 2**30:.1f} GiB"


def report_low_disk(reading: DiskReading, severity: str) -> None:
    """Send one Sentry event, grouped into one issue per severity.

    ``capture_message`` under a fixed fingerprint, as in
    ``apps.common.capacity``: a ``logger.warning`` would also become an event,
    but grouped by message text, which carries the changing numbers. Warning and
    critical are separate issues so that crossing into critical is a new issue,
    which is what the first-seen ``#scout-ops`` rule pages on.
    """
    message = (
        f"Host disk low: {reading.free_percent:.1f}% free "
        f"({_size(reading.free_bytes)} of {_size(reading.total_bytes)})"
    )
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["storage-headroom", "host-disk", severity]
        scope.set_tag("storage_severity", severity)
        scope.set_context(
            "storage",
            {
                "path": reading.path,
                "total_bytes": reading.total_bytes,
                "free_bytes": reading.free_bytes,
                "free_percent": round(reading.free_percent, 2),
                "warning_free_percent": settings.STORAGE_DISK_WARNING_FREE_PERCENT,
                "critical_free_percent": settings.STORAGE_DISK_CRITICAL_FREE_PERCENT,
                "critical_free_gib": settings.STORAGE_DISK_CRITICAL_FREE_GIB,
            },
        )
        sentry_sdk.capture_message(message, level="error" if severity == CRITICAL else "warning")


def _platform_db_sizes() -> tuple[int, list[tuple[str, int]]]:
    # Django has no async raw-cursor API.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        (total,) = cursor.fetchone()
        cursor.execute(_LARGEST_TABLES_SQL, [_LARGEST_TABLES])
        largest = [(name, int(size)) for name, size in cursor.fetchall()]
    return int(total), largest


async def check_storage_headroom() -> dict:
    """Alert on low host disk; log both readings at INFO for trends."""
    result: dict = {}

    disk = read_disk(settings.STORAGE_DISK_PATH)
    severity = disk_severity(disk)
    logger.info(
        "Storage headroom: host disk %s free of %s (%.1f%%) at %s",
        _size(disk.free_bytes),
        _size(disk.total_bytes),
        disk.free_percent,
        disk.path,
    )
    if severity:
        report_low_disk(disk, severity)
    result["disk_free_percent"] = round(disk.free_percent, 2)
    result["disk_severity"] = severity

    # A database that will not answer is reported by every request that uses it;
    # it must not stop the disk alert above, which has already gone.
    try:
        db_bytes, largest = await sync_to_async(_platform_db_sizes)()
    except Exception:
        logger.warning("Storage headroom: could not read the platform DB size", exc_info=True)
        return result
    # The largest tables show which one is growing (chat attachments, checkpoints,
    # telemetry) without this check knowing their names.
    logger.info(
        "Storage headroom: platform DB %s; largest tables: %s",
        _size(db_bytes),
        ", ".join(f"{name}={_size(size)}" for name, size in largest),
    )
    result["db_bytes"] = db_bytes
    return result
