"""Message loader for Open Chat Studio.

OCS does not expose a direct messages endpoint — messages are embedded in
the session detail response. This loader walks the session list and fetches
each session's detail (N+1), a bounded number at a time (#918).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor

from django.conf import settings
from django.db import connections

from mcp_server.loaders.ocs_base import OCS_MAX_PAGE_SIZE, OCSBaseLoader, OCSExportError

logger = logging.getLogger(__name__)

# Serial detail fetches made a 5k-session chatbot a ~25 min load (#918). The
# per-load concurrency (settings.OCS_DETAIL_FETCH_CONCURRENCY, default 8) stays
# under the session adapter's default pool of 10 connections so no request waits
# on a socket; the per-process limit keeps several concurrent loads from
# multiplying it against OCS. A slot is held through _get_json's Retry-After
# sleeps and its shared 401 token refresh, so a 429 or a refresh slows every load
# in the process, not just one.
# requests' HTTPAdapter default pool_maxsize (ocs_base.py mounts it unchanged).
_ADAPTER_POOL_SIZE = 10
_process_slots_lock = threading.Lock()
_process_slots: tuple[int, threading.BoundedSemaphore] | None = None


def detail_fetch_concurrency() -> int:
    """Per-load threads; more than the process limit would only sit idle."""
    return max(
        1,
        min(
            settings.OCS_DETAIL_FETCH_CONCURRENCY,
            settings.OCS_DETAIL_FETCH_PROCESS_LIMIT,
            _ADAPTER_POOL_SIZE,
        ),
    )


def detail_fetch_window() -> int:
    """Fetched-but-unyielded details are held in memory; cap the read-ahead."""
    return detail_fetch_concurrency() * 2


def _process_fetch_slots() -> threading.BoundedSemaphore:
    global _process_slots
    limit = max(1, settings.OCS_DETAIL_FETCH_PROCESS_LIMIT)
    with _process_slots_lock:
        # Only test settings change the limit; holders of a replaced semaphore
        # release into it, so the cap is briefly old + new.
        if _process_slots is None or _process_slots[0] != limit:
            _process_slots = (limit, threading.BoundedSemaphore(limit))
        return _process_slots[1]


class OCSMessageLoader(OCSBaseLoader):
    """Fetch messages for every session in an experiment.

    Two-pass: first walk the (cheap) session list collecting session ids,
    then fetch each session's detail. The list walk costs the same requests
    as the old interleaved approach but lets us know the session count up
    front, so the expensive detail-fetch phase can report determinate
    progress (issue #221).

    ``load_pages`` yields exactly one ``(rows, total_sessions)`` tuple per
    session — ``rows`` may be empty — where the total is denominated in
    **sessions**, not message rows (OCS' cursor pagination exposes no
    message count). The writer counts tuples to report per-session progress.
    """

    def load_pages(self) -> Iterator[tuple[list[dict], int | None]]:
        list_url = f"{self.base_url}/api/sessions/"
        # Max page size for the session-list walk to cut its request volume
        # ~10-15x (arch #254, finding 13#1); the per-session detail fetches
        # (the N+1) are unavoidable and dominate regardless.
        params = {"experiment": self.experiment_id, "page_size": OCS_MAX_PAGE_SIZE}
        session_ids: list[str] = []
        idless = 0
        for session_page, _session_total in self._paginate(list_url, params=params):
            for session in session_page:
                session_id = str(session.get("id") or "")
                if session_id:
                    session_ids.append(session_id)
                else:
                    idless += 1
        if idless:
            # Match the writers' natural-key guard: skip, but never silently (#263, 02#7).
            logger.warning("Skipped messages for %d sessions with no id", idless)

        # A live pagination walk can repeat a session; load one snapshot per id.
        session_ids = list(dict.fromkeys(session_ids))
        total_sessions = len(session_ids)
        total_messages = 0
        for session_id, messages in self._fetch_session_messages(session_ids):
            rows = map_session_messages(session_id, messages)
            total_messages += sum(not row["is_synthetic_summary"] for row in rows)
            yield rows, total_sessions
        logger.info(
            "Fetched %d messages across %d sessions for experiment %s",
            total_messages,
            total_sessions,
            self.experiment_id,
        )

    def _fetch_session_messages(self, session_ids: list[str]) -> Iterator[tuple[str, list]]:
        """Yield ``(session_id, messages)`` in ``session_ids`` order.

        Details are fetched concurrently but yielded in list order, so the
        output (and the writer's progress) is identical to a serial walk. The
        first failure in list order propagates; when the consumer stops early
        (an error or a cancel raised from progress), queued fetches are
        cancelled and the in-flight ones are abandoned rather than awaited.
        """
        pending: deque[tuple[str, Future]] = deque()
        remaining = iter(session_ids)
        window = detail_fetch_window()
        stopped = threading.Event()
        executor = ThreadPoolExecutor(
            max_workers=detail_fetch_concurrency(), thread_name_prefix="ocs-session-detail"
        )
        try:
            for session_id in remaining:
                pending.append(
                    (session_id, executor.submit(self._session_messages, session_id, stopped))
                )
                if len(pending) >= window:
                    break
            while pending:
                session_id, future = pending.popleft()
                messages = future.result()
                next_id = next(remaining, None)
                if next_id is not None:
                    pending.append(
                        (next_id, executor.submit(self._session_messages, next_id, stopped))
                    )
                yield session_id, messages
        finally:
            # A thread already parked on a process slot is past cancel_futures'
            # reach; this stops it from fetching once it gets the slot.
            stopped.set()
            executor.shutdown(wait=False, cancel_futures=True)

    def _session_messages(self, session_id: str, stopped: threading.Event) -> list:
        # A session with no messages legitimately omits/empties the key, so a
        # missing ``messages`` is treated as empty (not an error) — but the JSON
        # parse itself is validated via _get_json (finding 03#6).
        try:
            with _process_fetch_slots():
                if stopped.is_set():
                    return []
                detail = self._get_json(f"{self.base_url}/api/sessions/{session_id}/")
        finally:
            # A mid-run token refresh persists through the ORM; without this the
            # pool thread's DB connection would outlive the load (shared RDS).
            connections.close_all()
        return detail.get("messages") or []

    def load(self) -> list[dict]:
        return [row for page, _ in self.load_pages() for row in page]


def _revision(value: object) -> str:
    try:
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise OCSExportError("Session messages contain invalid JSON values.") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def map_session_messages(session_id: str, messages: list[dict]) -> list[dict]:
    """Any history change invalidates positional labels, including duplicate rows.

    OCS exposes no durable message ID. A session-wide revision deliberately
    invalidates earlier labels even on append; it must not pretend to be a
    source identity or match the old unguarded ``session:index`` namespace.
    """
    if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise OCSExportError("Session messages must be a list of message objects.")
    # Ignore upstream fields we do not store: export-only metadata must not
    # invalidate labels on otherwise unchanged rows.
    projected = [_project_message(raw) for raw in messages]
    revision = _revision([session_id, projected])
    return [_map_message(session_id, index, row, revision) for index, row in enumerate(projected)]


def _is_synthetic_summary(row: dict) -> bool:
    """True for the in-memory summary OCS interleaves into ``messages``.

    OCS ``ChatMessage.make_summary_message`` emits ``role=system`` with
    ``metadata.compression_marker == "summarize"``; the payload carries no
    explicit type field. The marker alone is not enough: a real, saved message
    can also carry it (checkpoint sentinel), but those are user/assistant turns,
    so requiring the system role keeps them counted.
    """
    metadata = row["metadata"]
    if not isinstance(metadata, dict):
        return False
    return row["role"] == "system" and metadata.get("compression_marker") == "summarize"


def _map_message(session_id: str, index: int, row: dict, revision: str) -> dict:
    return {
        "message_id": f"{session_id}:v2:{revision}:{index}",
        "snapshot_revision": revision,
        "message_version": _revision(row),
        "session_id": session_id,
        "message_index": index,
        "is_synthetic_summary": _is_synthetic_summary(row),
        **row,
    }


def _project_message(raw: dict) -> dict:
    return {
        "role": raw.get("role") or "",
        "content": raw.get("content") or "",
        "created_at": raw.get("created_at"),
        "metadata": raw.get("metadata") or {},
        "tags": raw.get("tags") or [],
    }
