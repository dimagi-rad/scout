"""Completed works data loader for CommCare Connect.

Fetches completed work records from the Connect CSV export endpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectCompletedWorkLoader(ConnectBaseLoader):
    """Fetch completed work data from Connect."""

    def load_pages(self) -> Iterator[list[dict]]:
        url = self._opp_url("completed_works/")
        rows = self._get_csv(url)
        logger.info(
            "Fetched %d completed works for opportunity %s", len(rows), self.opportunity_id
        )
        if rows:
            yield rows

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
