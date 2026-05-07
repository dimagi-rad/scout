"""Experiment (chatbot) loader for Open Chat Studio."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.ocs_base import OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSExperimentLoader(OCSBaseLoader):
    """Fetch the single chatbot (experiment) for this tenant."""

    def load_pages(self) -> Iterator[tuple[list[dict], int | None]]:
        url = f"{self.base_url}/api/experiments/{self.experiment_id}/"
        resp = self._get(url)
        data = resp.json()
        row = {
            "experiment_id": str(data.get("id") or self.experiment_id),
            "name": data.get("name") or "",
            "url": data.get("url") or "",
            "version_number": data.get("version_number"),
        }
        logger.info("Fetched experiment %s", self.experiment_id)
        yield [row], 1

    def load(self) -> list[dict]:
        return [row for page, _ in self.load_pages() for row in page]
