"""Invoice data loader for CommCare Connect.

Fetches invoice records from the Connect CSV export endpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectInvoiceLoader(ConnectBaseLoader):
    """Fetch invoice data from Connect."""

    def load_pages(self) -> Iterator[list[dict]]:
        url = self._opp_url("invoice/")
        rows = self._get_csv(url)
        logger.info("Fetched %d invoices for opportunity %s", len(rows), self.opportunity_id)
        if rows:
            yield rows

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
