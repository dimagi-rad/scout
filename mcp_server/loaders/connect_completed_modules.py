"""Completed module data loader for CommCare Connect.

Fetches completed module records from the v2 paginated JSON export endpoint
(``/export/opportunity/<id>/completed_module/``) and yields them unchanged —
the writer's typed columns accept native JSON values directly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectCompletedModuleLoader(ConnectBaseLoader):
    """Fetch completed module data from Connect."""

    def load_pages(self) -> Iterator[list[dict]]:
        total = 0
        for page in self._paginate_export_pages("completed_module/"):
            if not page:
                continue
            total += len(page)
            yield page
        logger.info("Fetched %d completed modules for opportunity %s", total, self.opportunity_id)

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
