"""Session loader for Open Chat Studio."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSSessionLoader(OCSBaseLoader):
    """Fetch chat sessions for this experiment (paginated)."""

    def load_pages(self) -> Iterator[list[dict]]:
        url = f"{self.base_url}/api/sessions/"
        params = {"experiment": self.experiment_id}
        total = 0
        for raw_page in self._paginate(url, params=params):
            rows = [_map_session(item) for item in raw_page]
            if not rows:
                continue
            total += len(rows)
            yield rows
        logger.info("Fetched %d sessions for experiment %s", total, self.experiment_id)

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]


def _map_session(raw: dict) -> dict:
    participant = raw.get("participant") or {}
    # OCS returns "experiment" as a nested object {id, name, url, ...} on
    # the list endpoint; extract just the id. Fall back to the raw value if
    # it's already a string (defensive).
    experiment = raw.get("experiment")
    if isinstance(experiment, dict):
        experiment_id = str(experiment.get("id") or "")
    else:
        experiment_id = str(experiment or "")
    return {
        "session_id": str(raw.get("id") or ""),
        "experiment_id": experiment_id,
        "participant_identifier": participant.get("identifier") or "",
        "participant_platform": participant.get("platform") or "",
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "tags": raw.get("tags") or [],
    }
