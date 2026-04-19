"""Participant loader for Open Chat Studio.

No dedicated participants endpoint — extract unique participants from the
session list.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSParticipantLoader(OCSBaseLoader):
    """Deduplicate participants from session list data."""

    def load_pages(self) -> Iterator[list[dict]]:
        url = f"{self.base_url}/api/sessions/"
        params = {"experiment": self.experiment_id}
        seen: set[str] = set()
        for session_page in self._paginate(url, params=params):
            rows: list[dict] = []
            for session in session_page:
                participant = session.get("participant") or {}
                identifier = participant.get("identifier")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                rows.append(
                    {
                        "identifier": identifier,
                        "platform": participant.get("platform") or "",
                        "remote_id": participant.get("remote_id") or "",
                    }
                )
            if rows:
                yield rows
        logger.info(
            "Fetched %d unique participants for experiment %s",
            len(seen),
            self.experiment_id,
        )

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
