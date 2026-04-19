"""Message loader for Open Chat Studio.

OCS does not expose a direct messages endpoint — messages are embedded in
the session detail response. This loader walks the session list and fetches
each session's detail (N+1). Acceptable given typical chatbot volumes per
the design spec.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSMessageLoader(OCSBaseLoader):
    """Fetch messages for every session in an experiment."""

    def load_pages(self) -> Iterator[list[dict]]:
        list_url = f"{self.base_url}/api/sessions/"
        params = {"experiment": self.experiment_id}
        total_sessions = 0
        total_messages = 0
        for session_page in self._paginate(list_url, params=params):
            for session in session_page:
                session_id = str(session.get("id") or "")
                if not session_id:
                    continue
                total_sessions += 1
                detail_url = f"{self.base_url}/api/sessions/{session_id}/"
                detail_resp = self._get(detail_url)
                messages = detail_resp.json().get("messages") or []
                rows = [_map_message(session_id, idx, msg) for idx, msg in enumerate(messages)]
                if rows:
                    total_messages += len(rows)
                    yield rows
        logger.info(
            "Fetched %d messages across %d sessions for experiment %s",
            total_messages,
            total_sessions,
            self.experiment_id,
        )

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]


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
