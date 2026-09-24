"""Message loader for Open Chat Studio.

OCS does not expose a direct messages endpoint — messages are embedded in
the session detail response. This loader walks the session list and fetches
each session's detail (N+1). Acceptable given typical chatbot volumes per
the design spec.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCS_MAX_PAGE_SIZE, OCSBaseLoader, OCSExportError

logger = logging.getLogger(__name__)


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
        for session_page, _session_total in self._paginate(list_url, params=params):
            for session in session_page:
                session_id = str(session.get("id") or "")
                if session_id:
                    session_ids.append(session_id)

        # A live pagination walk can repeat a session; load one snapshot per id.
        session_ids = list(dict.fromkeys(session_ids))
        total_sessions = len(session_ids)
        total_messages = 0
        for session_id in session_ids:
            detail_url = f"{self.base_url}/api/sessions/{session_id}/"
            # A session with no messages legitimately omits/empties the key, so
            # a missing ``messages`` is treated as empty (not an error) — but the
            # JSON parse itself is validated via _get_json (finding 03#6).
            messages = self._get_json(detail_url).get("messages") or []
            rows = map_session_messages(session_id, messages)
            total_messages += len(rows)
            yield rows, total_sessions
        logger.info(
            "Fetched %d messages across %d sessions for experiment %s",
            total_messages,
            total_sessions,
            self.experiment_id,
        )

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


def _map_message(session_id: str, index: int, row: dict, revision: str) -> dict:
    return {
        "message_id": f"{session_id}:v2:{revision}:{index}",
        "snapshot_revision": revision,
        "message_version": _revision(row),
        "session_id": session_id,
        "message_index": index,
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
