"""Connection-capacity exhaustion: one classification, one user response, one alert.

Prod and staging share one RDS instance, and the API's Django connections, the
LangGraph checkpointer pool and Cube's pools all draw from its
``max_connections``. When that ceiling is hit, each layer fails in its own
dialect — a libpq ``FATAL`` at connect, a ``PoolTimeout``, a Cube error body —
and before this module each surfaced as a 500 or a red "server unreachable" bar.

``classify_capacity_error`` recognises every dialect and returns one
``CapacityExhausted``. Callers turn that into a calm, retryable "Scout is busy"
response (``busy_response`` or the chat stream's busy event) and call
``report_capacity_exhausted``, which is the human escalation: at most one
ERROR-level Sentry event per resource per ``ALERT_WINDOW_SECONDS``, under a stable
fingerprint so it groups into a single alertable issue instead of a flood.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import StrEnum

import psycopg
import sentry_sdk
from django.core.cache import cache
from django.db import OperationalError as DjangoOperationalError
from django.db import connection
from django.http import JsonResponse

from apps.common.error_codes import ErrorCode
from apps.common.errors import ExpectedStateError

logger = logging.getLogger(__name__)

BUSY_ERROR = "busy"
BUSY_MESSAGE = "Scout is busy right now. Please try again in a few seconds."
RETRY_AFTER_SECONDS = 5
ALERT_WINDOW_SECONDS = 15 * 60
ALERT_MESSAGE = (
    "Connection limit reached — consider raising RDS max_connections / instance size or pool caps"
)

# SQLSTATE 53300 is too_many_connections. A refusal at connect time usually
# arrives without a SQLSTATE, as libpq FATAL text, so the text is matched too;
# this is the edge of a system Scout does not own (errors.py reporting rule 5).
TOO_MANY_CONNECTIONS_SQLSTATE = "53300"
CAPACITY_ERROR_MARKERS = (
    "too many clients already",
    "remaining connection slots are reserved",
    "too many connections",
)


class CapacityResource(StrEnum):
    DATABASE = "db"
    CHECKPOINTER_POOL = "checkpointer_pool"
    CUBE = "cube"


class CapacityExhausted(ExpectedStateError):
    """A shared connection limit is full; the request is safe to retry shortly.

    Expected under ``apps.common.errors``' test: known (each layer names the
    condition), routine (bursts of concurrent chats fill a fixed ceiling),
    surfaced (the user gets a retrying "busy" notice, and operators get the
    rate-limited event from ``report_capacity_exhausted`` — a deliberate
    message, not this exception), and resolved (the retry, or raising the cap).
    """

    code = ErrorCode.CAPACITY_EXHAUSTED

    def __init__(self, resource: CapacityResource, detail: str = ""):
        super().__init__(detail or f"{resource} connection capacity exhausted")
        self.resource = resource


def is_capacity_message(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CAPACITY_ERROR_MARKERS)


def _classify_one(exc: BaseException) -> CapacityExhausted | None:
    if isinstance(exc, CapacityExhausted):
        return exc
    # Boundary adapters (e.g. the Cube client) tag their own capacity failures.
    resource = getattr(exc, "capacity_resource", None)
    if resource:
        return CapacityExhausted(CapacityResource(resource), str(exc))
    if isinstance(exc, DjangoOperationalError | psycopg.OperationalError):
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate == TOO_MANY_CONNECTIONS_SQLSTATE or is_capacity_message(str(exc)):
            return CapacityExhausted(CapacityResource.DATABASE, str(exc))
    return None


def classify_capacity_error(exc: BaseException) -> CapacityExhausted | None:
    """Return a ``CapacityExhausted`` if ``exc`` or anything it wraps is one.

    Only ``__cause__`` is followed (Django's wrapping and explicit ``raise ...
    from``). A bug raised while *handling* a capacity error has it as
    ``__context__`` and must stay a bug, as in ``config.sentry.before_send``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        found = _classify_one(current)
        if found is not None:
            return found
        current = current.__cause__
    return None


def busy_payload() -> dict:
    return {"error": BUSY_ERROR, "code": ErrorCode.CAPACITY_EXHAUSTED, "message": BUSY_MESSAGE}


def busy_response() -> JsonResponse:
    response = JsonResponse(busy_payload(), status=503)
    response["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response


# Fallback only for when the shared cache itself is unreachable; per process, so a
# cache outage can at worst multiply the alert by the worker count, never flood.
_local_alert_deadlines: dict[str, float] = {}
_local_alert_lock = threading.Lock()


def _claim_alert_window(resource: str) -> bool:
    try:
        return bool(cache.add(f"capacity-exhausted-alert:{resource}", 1, ALERT_WINDOW_SECONDS))
    except Exception:
        logger.warning(
            "Capacity alert cache unavailable; using a per-process window", exc_info=True
        )
        now = time.monotonic()
        with _local_alert_lock:
            if _local_alert_deadlines.get(resource, 0.0) > now:
                return False
            _local_alert_deadlines[resource] = now + ALERT_WINDOW_SECONDS
            return True


def _connection_usage() -> dict | None:
    """Current platform-DB connection count vs ``max_connections``, or ``None``.

    Only on a connection this thread already holds: opening one now would take a
    scarce slot, or block the thread on a refused connect, at the worst moment.
    """
    if connection.connection is None:
        return None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT (SELECT count(*) FROM pg_stat_activity), "
                "current_setting('max_connections')::int"
            )
            active, maximum = cursor.fetchone()
    except Exception:
        logger.debug("Could not read connection usage for the capacity alert", exc_info=True)
        return None
    return {"pg_stat_activity_count": active, "max_connections": maximum}


def report_capacity_exhausted(
    resource: CapacityResource | str,
    detail: str = "",
    *,
    exc_info: BaseException | None = None,
) -> None:
    """Log every occurrence; escalate to Sentry at most once per window per resource.

    Sync: it reads the cache and, when the database still answers, one row of
    connection stats. Async callers bridge it with ``sync_to_async``.
    """
    resource = CapacityResource(resource)
    logger.warning("Connection capacity exhausted: %s: %s", resource, detail, exc_info=exc_info)
    if not _claim_alert_window(resource):
        return

    usage = _connection_usage()
    details = {"resource": str(resource), "detail": detail, **(usage or {"usage": "unavailable"})}
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["capacity-exhausted", str(resource)]
        scope.set_tag("capacity_resource", str(resource))
        scope.set_context("capacity", details)
        sentry_sdk.capture_message(f"{ALERT_MESSAGE} ({resource})", level="error")
