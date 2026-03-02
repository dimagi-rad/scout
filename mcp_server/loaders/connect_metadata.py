"""Metadata loader for CommCare Connect.

Fetches opportunity detail and org/program structure from the Connect API.
This metadata is stored in TenantMetadata for reference.
"""

from __future__ import annotations

import logging

from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectMetadataLoader(ConnectBaseLoader):
    """Fetch metadata for a Connect opportunity."""

    def load(self) -> dict:
        org_data = self._fetch_org_data()
        opp_detail = self._fetch_opportunity_detail()

        logger.info(
            "Loaded metadata for Connect opportunity %s: %s",
            self.opportunity_id,
            opp_detail.get("name", "unknown"),
        )

        return {
            "opportunity": opp_detail,
            "organizations": org_data.get("organizations", []),
            "programs": org_data.get("programs", []),
            "all_opportunities": org_data.get("opportunities", []),
        }

    def _fetch_org_data(self) -> dict:
        url = f"{self.base_url}/export/opp_org_program_list/"
        resp = self._get(url)
        return resp.json()

    def _fetch_opportunity_detail(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/"
        resp = self._get(url)
        return resp.json()
