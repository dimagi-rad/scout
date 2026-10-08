"""Write telemetry events without ever failing or slowing the caller.

Every entry point swallows and logs its own errors: a broken telemetry write must
never surface in a chat turn or a request. Writes go through the caller's
existing Django connection; nothing here opens a connection or a pool, because
the platform database is shared and has run out of connections before.

Callers must await these inline, inside the request or task they measure. A
write spawned with ``create_task`` that outlives its request would open a fresh
connection in a thread nothing closes.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime
from itertools import islice
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction

from apps.telemetry.models import TelemetryEvent

logger = logging.getLogger(__name__)

# Labels (tool names, phases, error codes, model ids) fit this; sentences, emails
# and SQL do not, so free text cannot ride along in an event.
_LABEL = re.compile(r"[A-Za-z0-9_.:/-]{1,64}")
_KEY = re.compile(r"[a-z0-9_]{1,40}")
MAX_ATTRS = 32
# duration_ms is an int4 column.
MAX_DURATION_MS = 2_147_483_647


def _enabled() -> bool:
    return getattr(settings, "TELEMETRY_ENABLED", True)


def is_label(value: Any) -> bool:
    return isinstance(value, str) and bool(_LABEL.fullmatch(value))


def _clean_value(value: Any) -> tuple[bool, Any]:
    if value is None or isinstance(value, bool | int):
        return True, value
    if isinstance(value, float):
        return math.isfinite(value), value
    if isinstance(value, uuid.UUID):
        return True, str(value)
    return is_label(value), value


def _clean_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only counts, flags and labels under short snake_case keys."""
    clean: dict[str, Any] = {}
    if not isinstance(attrs, dict):
        return clean
    for key, value in islice(attrs.items(), MAX_ATTRS):
        if not (isinstance(key, str) and _KEY.fullmatch(key)):
            continue
        keep, value = _clean_value(value)
        if keep:
            clean[key] = value
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


def _as_user_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _as_duration(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return min(MAX_DURATION_MS, max(0, round(value)))


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
) -> TelemetryEvent | None:
    """An unsaved event, or None if it cannot be built; never raises."""
    try:
        fields: dict[str, Any] = {
            "kind": kind if is_label(kind) else "unknown",
            "user_id": _as_user_id(user_id),
            "workspace_id": _as_uuid(workspace_id),
            "name": name if is_label(name) else "",
            "outcome": outcome if is_label(outcome) and len(outcome) <= 16 else "",
            "duration_ms": _as_duration(duration_ms),
            "attrs": _clean_attrs(attrs),
        }
        if isinstance(occurred_at, datetime):
            fields["occurred_at"] = occurred_at
        return TelemetryEvent(**fields)
    except Exception:
        logger.warning("Could not build telemetry event", exc_info=True)
        return None


@sync_to_async
def _insert(rows: list[TelemetryEvent]) -> None:
    # A savepoint, as in ``record``: an insert that fails inside an open
    # transaction must not leave it aborted for the caller.
    with transaction.atomic():
        TelemetryEvent.objects.bulk_create(rows)


async def arecord_events(events: list[TelemetryEvent | None]) -> None:
    """Insert ``events`` in one statement; never raises."""
    if not _enabled():
        return
    rows = [event for event in events if event is not None]
    if not rows:
        return
    try:
        await _insert(rows)
    except Exception:
        logger.warning("Could not record %d telemetry event(s)", len(rows), exc_info=True)


def _safe_build(kind: str, fields: dict[str, Any]) -> TelemetryEvent | None:
    # A stale or misspelt keyword fails at call binding, before build_event's own guard.
    try:
        return build_event(kind, **fields)
    except TypeError:
        logger.warning("Bad telemetry fields for %s", kind, exc_info=True)
        return None


async def arecord(kind: str, **fields: Any) -> None:
    """Record one event from async code; never raises."""
    if not _enabled():
        return
    await arecord_events([_safe_build(kind, fields)])


def record(kind: str, **fields: Any) -> None:
    """Record one event from sync code; never raises.

    The savepoint keeps a failed insert from breaking a caller's open transaction.
    """
    if not _enabled():
        return
    event = _safe_build(kind, fields)
    if event is None:
        return
    try:
        with transaction.atomic():
            event.save()
    except Exception:
        logger.warning("Could not record telemetry event %s", kind, exc_info=True)
