"""Metadata discovery loader for Open Chat Studio."""

from __future__ import annotations

import logging

from mcp_server.loaders.ocs_base import OCSBaseLoader

logger = logging.getLogger(__name__)


class OCSMetadataLoader(OCSBaseLoader):
    """Fetch experiment detail for TenantMetadata storage."""

    def load(self) -> dict:
        url = f"{self.base_url}/api/experiments/{self.experiment_id}/"
        resp = self._get(url)
        detail = resp.json()
        logger.info(
            "Loaded metadata for OCS experiment %s: %s",
            self.experiment_id,
            detail.get("name", "unknown"),
        )
        return {"experiment": detail}
