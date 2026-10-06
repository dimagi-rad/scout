"""CSV export of the data behind an artifact (#846).

Each request exports one dataset: a live semantic query the artifact declares,
or a tabular entry of its static ``data``. Cube's /load cannot stream, so the
whole result (up to EXPORT_ROW_LIMIT rows) is in memory before the first byte;
only the CSV encoding is chunked. Memory is bounded by one dataset per request
and MAX_CONCURRENT_EXPORTS per process, not by streaming; past the slot TTL the
bound is best-effort (a download slower than that loses its slot). A zip of every
dataset would hold all of them at once.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.http import JsonResponse, StreamingHttpResponse

from apps.semantic.services.query import run_semantic_query

from .query_batch import ArtifactQueryPlan, PlannedQuery

# Cube's default CUBEJS_DB_QUERY_LIMIT; a larger limit is refused by Cube itself.
EXPORT_ROW_LIMIT = 50_000
# Cube's own budget is 60s (QUERY_TOTAL_TIMEOUT_SECONDS); this also bounds
# catalog compilation and the workspace-context lookup.
EXPORT_TIMEOUT_SECONDS = 90
CSV_CHUNK_ROWS = 1_000
# Per process: an export holds a tenant Cube connection slot and up to 50k rows
# in memory, so a burst of exports must not starve dashboard queries.
MAX_CONCURRENT_EXPORTS = 2
# The workspace lock spans only the query (EXPORT_TIMEOUT_SECONDS), so a lock no
# release ever freed (a killed process, a failed Redis delete) clears soon after.
EXPORT_LOCK_TTL_SECONDS = EXPORT_TIMEOUT_SECONDS + 30
# Reclaims a process slot whose response Django dropped without close(). A live
# download slower than this can be reclaimed too, admitting extra exports while
# it finishes, so the cap is best-effort past this horizon.
EXPORT_SLOT_TTL_SECONDS = 600
EXPORT_TIMEOUT_MESSAGE = "The export took too long. Add filters to narrow it and try again."

logger = logging.getLogger(__name__)

QUERY_SOURCE = "query"
STATIC_SOURCE = "static"

# Spreadsheet apps evaluate a cell starting with these as a formula (OWASP CSV injection).
# Fullwidth forms are evaluated as formulas by some Excel locales.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", re.ASCII)


@dataclass(frozen=True)
class TabularData:
    columns: list[str]
    rows: list[list[Any]]


def csv_safe_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict | list):
        text = json.dumps(value, default=str)
    else:
        text = value if isinstance(value, str) else str(value)
    # Some importers trim before evaluating, so leading whitespace does not hide a formula.
    # Cube returns numeric measures as strings; a negative number is data, not a formula.
    stripped = text.lstrip(" \n")
    if stripped.startswith(_FORMULA_PREFIXES) and not _PLAIN_NUMBER.fullmatch(stripped):
        return "'" + text
    return text


async def iter_csv(columns: Iterable[Any], rows: Iterable[Iterable[Any]]) -> AsyncIterator[bytes]:
    """Yield an RFC 4180 CSV (CRLF, minimal quoting) with a UTF-8 BOM for Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    buffer.write("﻿")
    writer.writerow([csv_safe_cell(column) for column in columns])
    pending = 0
    for row in rows:
        writer.writerow([csv_safe_cell(cell) for cell in row])
        pending += 1
        if pending >= CSV_CHUNK_ROWS:
            yield buffer.getvalue().encode()
            buffer.seek(0)
            buffer.truncate()
            pending = 0
            # Let other requests on this event loop run between chunks.
            await asyncio.sleep(0)
    remainder = buffer.getvalue()
    if remainder:
        yield remainder.encode()


def static_tabular_datasets(data: Any) -> dict[str, TabularData]:
    """Entries of ``artifact.data`` that are tables: a list of records, or columns + rows."""
    if not isinstance(data, dict):
        return {}
    tables = {}
    for key, value in data.items():
        table = _as_table(value)
        if table is not None:
            tables[str(key)] = table
    return tables


def _as_table(value: Any) -> TabularData | None:
    if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        columns: list[str] = []
        seen: set[str] = set()
        for record in value:
            for column in record:
                if column not in seen:
                    seen.add(column)
                    columns.append(column)
        return TabularData(
            columns, [[record.get(column) for column in columns] for record in value]
        )
    if (
        isinstance(value, dict)
        and isinstance(value.get("columns"), list)
        and isinstance(value.get("rows"), list)
        and all(isinstance(row, list) for row in value["rows"])
    ):
        return TabularData([str(c) for c in value["columns"]], value["rows"])
    return None


def export_datasets(plan: ArtifactQueryPlan, data: Any) -> list[dict[str, str]]:
    seen: set[str] = set()
    datasets = []
    for planned in plan.queries:
        if planned.spec is not None and planned.name not in seen:
            seen.add(planned.name)
            datasets.append({"name": planned.name, "source": QUERY_SOURCE})
    datasets.extend(
        {"name": name, "source": STATIC_SOURCE} for name in static_tabular_datasets(data)
    )
    return datasets


def find_planned_query(plan: ArtifactQueryPlan, name: str) -> PlannedQuery | None:
    return next(
        (q for q in plan.queries if q.name == name and q.spec is not None),
        None,
    )


async def run_export_query(workspace, planned: PlannedQuery, *, user_id: str) -> dict[str, Any]:
    """Run one declared query with the export cap in place of its display limit.

    The artifact's own ``limit`` sizes a chart, not the data behind it, so it is
    replaced. Raises ``TimeoutError`` past ``EXPORT_TIMEOUT_SECONDS``.
    """
    spec = {**planned.spec, "limit": EXPORT_ROW_LIMIT}
    async with asyncio.timeout(EXPORT_TIMEOUT_SECONDS):
        return await run_semantic_query(
            workspace, spec, user_id=user_id, max_limit=EXPORT_ROW_LIMIT
        )


# lease id -> monotonic acquire time. release() also runs in the worker thread
# that calls StreamingHttpResponse.close(), hence the lock.
_active_leases: dict[str, float] = {}
_leases_lock = threading.Lock()


def active_exports() -> int:
    with _leases_lock:
        return len(_active_leases)


def _now() -> float:
    return time.monotonic()


def _claim_process_slot() -> str | None:
    """A slot id, or None when full. Slots older than EXPORT_SLOT_TTL_SECONDS are reclaimed:
    a response Django drops without close() (a disconnect during middleware)
    would otherwise hold its slot until the process restarts."""
    now = _now()
    with _leases_lock:
        for lease_id, acquired in list(_active_leases.items()):
            if now - acquired > EXPORT_SLOT_TTL_SECONDS:
                del _active_leases[lease_id]
        if len(_active_leases) >= MAX_CONCURRENT_EXPORTS:
            return None
        lease_id = secrets.token_hex(16)
        _active_leases[lease_id] = now
        return lease_id


def _free_process_slot(lease_id: str) -> bool:
    with _leases_lock:
        return _active_leases.pop(lease_id, None) is not None


class ExportLease:
    """A process slot, held until the CSV is sent, plus the workspace's export lock,
    held only while the Cube query runs: the lanes it protects are free once
    the query returns, and a slow download must not block the workspace's next export.

    The lock stores this lease's id, so a late release does not delete a lock that
    expired and was re-taken by another export (best-effort: get-then-delete is not
    atomic, and the lock TTL exceeds the query timeout).
    """

    def __init__(self, lease_id: str, lock_key: str) -> None:
        self.id = lease_id
        self._lock_key = lock_key
        self._slot_held = True
        self._lock_held = True
        self._guard = threading.Lock()

    def _take(self, attr: str) -> bool:
        with self._guard:
            held = getattr(self, attr)
            setattr(self, attr, False)
            return held

    async def arelease_lock(self) -> None:
        if self._take("_lock_held"):
            await _adelete_own_lock(self._lock_key, self.id)

    async def arelease(self) -> None:
        await self.arelease_lock()
        if self._take("_slot_held"):
            _free_process_slot(self.id)

    def release(self) -> None:
        """Idempotent; for sync callers such as response.close() in a worker thread."""
        if self._take("_lock_held"):
            _delete_own_lock(self._lock_key, self.id)
        if self._take("_slot_held"):
            _free_process_slot(self.id)


def _delete_own_lock(key: str, lease_id: str) -> None:
    try:
        if cache.get(key) == lease_id:
            cache.delete(key)
    except Exception:
        logger.warning("Could not release artifact export lock %s", key, exc_info=True)


async def _adelete_own_lock(key: str, lease_id: str) -> None:
    try:
        if await cache.aget(key) == lease_id:
            await cache.adelete(key)
    except Exception:
        logger.warning("Could not release artifact export lock %s", key, exc_info=True)


# Strong references so the event loop does not garbage-collect pending cleanups.
_lock_cleanups: set[asyncio.Task] = set()


async def _free_lock_once_added(add: asyncio.Future, key: str, lease_id: str) -> None:
    try:
        locked = await add
    except Exception:
        logger.warning("Cancelled artifact export lock add failed: %s", key, exc_info=True)
        return
    if locked:
        await _adelete_own_lock(key, lease_id)


async def acquire_export_lease(workspace_id) -> ExportLease | None:
    """Claim a process slot and the workspace's export lock, or None when either is taken.

    Never waits, so a full house is answered at once with a busy 503. The process
    cap bounds memory (a result stays referenced until its last byte is sent); the
    cross-process workspace lock keeps exports from taking every Cube query lane
    of one tenant (DRIVER_POOL_MAX in cube_config/cube.js) away from its dashboards.
    """
    lease_id = _claim_process_slot()
    if lease_id is None:
        return None
    key = f"artifact-data-export:{workspace_id}"
    add = asyncio.ensure_future(cache.aadd(key, lease_id, EXPORT_LOCK_TTL_SECONDS))
    try:
        locked = await asyncio.shield(add)
    except Exception:
        # Fail closed: without the lock, exports could take the tenant's Cube lanes.
        logger.warning("Could not take artifact export lock %s", key, exc_info=True)
        _free_process_slot(lease_id)
        # The SET NX may have landed before the reply failed; a no-op otherwise.
        await _adelete_own_lock(key, lease_id)
        return None
    except BaseException:
        _free_process_slot(lease_id)
        # The add keeps running in its worker thread after a cancel; free the
        # lock once it lands rather than leave it ownerless for the TTL.
        cleanup = asyncio.ensure_future(_free_lock_once_added(add, key, lease_id))
        _lock_cleanups.add(cleanup)
        cleanup.add_done_callback(_lock_cleanups.discard)
        raise
    if not locked:
        _free_process_slot(lease_id)
        return None
    return ExportLease(lease_id, key)


async def leased_stream(chunks: AsyncIterator[bytes], lease: ExportLease) -> AsyncIterator[bytes]:
    try:
        async for chunk in chunks:
            yield chunk
    finally:
        await lease.arelease()


class LeasedStreamingResponse(StreamingHttpResponse):
    """Releases its lease on close(), which Django calls even if the body was never read."""

    def __init__(self, *args, lease: ExportLease | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lease = lease

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._lease is not None:
                self._lease.release()


def export_error_response(error: Any) -> JsonResponse:
    if not isinstance(error, dict):
        return JsonResponse({"error": str(error) or "The export query failed."}, status=502)
    category = error.get("category")
    message = error.get("message") or "The export query failed."
    if category == "data_unavailable":
        return JsonResponse({"error": message}, status=409)
    if category == "transient_runtime_failure":
        # Pinned to CubeClient's wording by test_cube_timeout_is_a_504.
        if "timed out" in message:
            return JsonResponse({"error": EXPORT_TIMEOUT_MESSAGE}, status=504)
        return JsonResponse({"error": message}, status=503)
    if category == "runtime_failure":
        return JsonResponse({"error": "The export query failed."}, status=500)
    return JsonResponse({"error": message}, status=502)


def audit_query_shape(query: Any) -> dict[str, Any]:
    """What was exported, without filter values: those can carry personal identifiers."""
    if not isinstance(query, dict):
        return {}
    return {
        "measures": query.get("measures"),
        "dimensions": query.get("dimensions"),
        "time_dimension": query.get("time_dimension"),
        "granularity": query.get("granularity"),
        "filters": [
            {"field": f.get("field") or f.get("member"), "operator": f.get("operator")}
            for f in query.get("filters") or []
            if isinstance(f, dict)
        ],
        "limit": query.get("limit"),
    }


def export_filename(title: str, dataset: str, *, truncated: bool) -> str:
    stem = "-".join(part for part in (_slug(title), _slug(dataset)) if part) or "artifact-data"
    if truncated:
        stem += f"-first-{EXPORT_ROW_LIMIT}-rows"
    return f"{stem}.csv"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()[:60]
