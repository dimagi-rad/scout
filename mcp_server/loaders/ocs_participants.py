"""Participant loader for Open Chat Studio.

Uses the dedicated ``GET /api/participants`` endpoint (added in OCS PR
dimagi/open-chat-studio#3334) to fetch rich participant records — including
``name`` and per-chatbot custom ``data`` — rather than deriving a minimal
participant list from the session endpoint.

The endpoint is cursor-paginated (``{"count": ..., "next": ..., "previous":
..., "results": [...]}``) — the first page now carries a ``count`` total (arch
#254, finding 13#1), so participant progress is determinate. It returns
``ParticipantDetail`` objects shaped as::

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

We pass ``experiment=<experiment_id>`` so the participant list (and each
participant's ``data`` array) is scoped to this tenant's chatbot. The OCS
``ParticipantView`` reads only the ``experiment`` query param
(``request.query_params.get("experiment")``); an earlier ``chatbot`` param was
silently ignored, returning the WHOLE team roster plus every chatbot's
per-participant ``data`` into a single-chatbot tenant schema (cross-chatbot
PII disclosure — arch #245 finding 12#3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCS_MAX_PAGE_SIZE, OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSParticipantLoader(OCSBaseLoader):
    """Fetch participants from the dedicated OCS participant endpoint."""

    def load_pages(self) -> Iterator[tuple[list[dict], int | None]]:
        url = f"{self.base_url}/api/participants"
        # Scope to this tenant's chatbot so the participant list and each
        # participant's per-chatbot ``data`` array are limited to it. The OCS
        # ParticipantView filters on the ``experiment`` query param; ``chatbot``
        # is silently ignored and leaks the whole team roster (arch #245).
        #
        # Request the max page size to cut list-request volume ~10-15x versus the
        # OCS default of 100 (arch #254, finding 13#1).
        params = {"experiment": self.experiment_id, "page_size": OCS_MAX_PAGE_SIZE}
        total = 0
        # Upstream now provides a first-page ``count`` (arch #254, finding 13#1);
        # ``_paginate`` surfaces it on the first tuple. Pass it through so
        # participant progress is determinate instead of always indeterminate.
        for raw_page, page_total in self._paginate(url, params=params):
            rows = [_map_participant(item) for item in raw_page]
            if not rows:
                continue
            total += len(rows)
            yield rows, page_total
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
