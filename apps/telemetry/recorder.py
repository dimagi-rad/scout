"""Write telemetry events without ever failing or slowing the caller.

Every entry point swallows and logs its own errors: a broken telemetry write must
never surface in a chat turn or a request. Writes go through the caller's
existing Django connection; nothing here opens a connection or a pool, because
the platform database is shared and has run out of connections before.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.telemetry.models import TelemetryEvent

logger = logging.getLogger(__name__)

# Long enough for a tool or phase name; short enough that free text cannot ride along.
MAX_ATTR_STRING = 64
MAX_ATTRS = 32


def _enabled() -> bool:
    return getattr(settings, "TELEMETRY_ENABLED", True)


def _clean_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only scalar attributes: counts, flags, short labels."""
    if not attrs:
        return {}
    clean: dict[str, Any] = {}
    for key, value in list(attrs.items())[:MAX_ATTRS]:
        short_label = isinstance(value, str) and len(value) <= MAX_ATTR_STRING
        if value is None or isinstance(value, bool | int | float) or short_label:
            clean[str(key)] = value
        elif isinstance(value, uuid.UUID):
            clean[str(key)] = str(value)
    return clean


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def build_event(
    kind: str,
    *,
    user_id: Any = None,
    workspace_id: Any = None,
    name: str = "",
    outcome: str = "",
    duration_ms: float | None = None,
    attrs: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> TelemetryEvent:
    """An unsaved event, for callers that collect several and write them once."""
    fields: dict[str, Any] = {
        "kind": kind,
        "user_id": _as_uuid(user_id),
        "workspace_id": _as_uuid(workspace_id),
        "name": (name or "")[:128],
        "outcome": (outcome or "")[:16],
        "duration_ms": max(0, round(duration_ms)) if duration_ms is not None else None,
        "attrs": _clean_attrs(attrs),
    }
    if occurred_at is not None:
        fields["occurred_at"] = occurred_at
    return TelemetryEvent(**fields)


async def arecord_events(events: list[TelemetryEvent]) -> None:
    """Insert ``events`` in one statement; never raises."""
    if not events or not _enabled():
        return
    try:
        await TelemetryEvent.objects.abulk_create(events)
    except Exception:
        logger.warning("Could not record %d telemetry event(s)", len(events), exc_info=True)


async def arecord(kind: str, **fields: Any) -> None:
    """Record one event from async code; never raises."""
    try:
        event = build_event(kind, **fields)
    except Exception:
        logger.warning("Could not build telemetry event %s", kind, exc_info=True)
        return
    await arecord_events([event])


def record(kind: str, **fields: Any) -> None:
    """Record one event from sync code; never raises.

    The savepoint keeps a failed insert from breaking a caller's open transaction.
    """
    if not _enabled():
        return
    try:
        event = build_event(kind, **fields)
        with transaction.atomic():
            event.save()
    except Exception:
        logger.warning("Could not record telemetry event %s", kind, exc_info=True)
