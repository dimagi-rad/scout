"""Invoice data loader for CommCare Connect.

Fetches invoice records from the v2 paginated JSON export endpoint
(``/export/opportunity/<id>/invoice/``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader, stringify_record

logger = logging.getLogger(__name__)


class ConnectInvoiceLoader(ConnectBaseLoader):
    """Fetch invoice data from Connect."""

    def load_pages(self) -> Iterator[list[dict]]:
        total = 0
        for page in self._paginate_export_pages("invoice/"):
            if not page:
                continue
            stringified = [stringify_record(r) for r in page]
            total += len(stringified)
            yield stringified
        logger.info("Fetched %d invoices for opportunity %s", total, self.opportunity_id)

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
