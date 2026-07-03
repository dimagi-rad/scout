"""Message loader for Open Chat Studio.

OCS does not expose a direct messages endpoint — messages are embedded in
the session detail response. This loader walks the session list and fetches
each session's detail (N+1). Acceptable given typical chatbot volumes per
the design spec.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCS_MAX_PAGE_SIZE, OCSBaseLoader

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

        total_sessions = len(session_ids)
        total_messages = 0
        for session_id in session_ids:
            detail_url = f"{self.base_url}/api/sessions/{session_id}/"
            # A session with no messages legitimately omits/empties the key, so
            # a missing ``messages`` is treated as empty (not an error) — but the
            # JSON parse itself is validated via _get_json (finding 03#6).
            messages = self._get_json(detail_url).get("messages") or []
            rows = [_map_message(session_id, idx, msg) for idx, msg in enumerate(messages)]
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


def _map_message(session_id: str, index: int, raw: dict) -> dict:
    return {
        "message_id": f"{session_id}:{index}",
        "session_id": session_id,
        "message_index": index,
        "role": raw.get("role") or "",
        "content": raw.get("content") or "",
        "created_at": raw.get("created_at"),
        "metadata": raw.get("metadata") or {},
        "tags": raw.get("tags") or [],
    }
