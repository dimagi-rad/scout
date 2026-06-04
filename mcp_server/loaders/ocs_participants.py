"""Participant loader for Open Chat Studio.

Uses the dedicated ``GET /api/participants`` endpoint (added in OCS PR
dimagi/open-chat-studio#3334) to fetch rich participant records — including
``name`` and per-chatbot custom ``data`` — rather than deriving a minimal
participant list from the session endpoint.

The endpoint is cursor-paginated (``{"next": ..., "previous": ...,
"results": [...]}``) and returns ``ParticipantDetail`` objects shaped as::

    {
        "id": "<participant public uuid>",
        "identifier": "part1",
        "name": "John",
        "platform": "api",
        "remote_id": "",
        "data": [
            {
                "chatbot": "Support Bot",
                "chatbot_id": "<experiment public uuid>",
                "data": {"name": "John", "timezone": "Africa/Johannesburg"},
            },
        ],
    }

We pass ``chatbot=<experiment_id>`` so the participant list (and each
participant's ``data`` array) is scoped to this tenant's chatbot.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSParticipantLoader(OCSBaseLoader):
    """Fetch participants from the dedicated OCS participant endpoint."""

    def load_pages(self) -> Iterator[tuple[list[dict], int | None]]:
        url = f"{self.base_url}/api/participants"
        # Scope to this tenant's chatbot so the participant list and each
        # participant's per-chatbot ``data`` array are limited to it.
        params = {"chatbot": self.experiment_id}
        total = 0
        # Cursor pagination — no ``count`` field, so totals are always None.
        for raw_page, _page_total in self._paginate(url, params=params):
            rows = [_map_participant(item) for item in raw_page]
            if not rows:
                continue
            total += len(rows)
            yield rows, None
        logger.info(
            "Fetched %d participants for experiment %s",
            total,
            self.experiment_id,
        )

    def load(self) -> list[dict]:
        return [row for page, _ in self.load_pages() for row in page]


def _map_participant(raw: dict) -> dict:
    return {
        "participant_id": str(raw.get("id") or ""),
        "identifier": raw.get("identifier") or "",
        "name": raw.get("name") or "",
        "platform": raw.get("platform") or "",
        "remote_id": raw.get("remote_id") or "",
        # Per-chatbot custom data entries: [{chatbot, chatbot_id, data}, ...].
        "data": raw.get("data") or [],
    }
