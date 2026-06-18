"""Metadata loader for CommCare Connect.

Fetches opportunity detail and org/program structure from the Connect API.
This metadata is stored in TenantMetadata for reference.
"""

from __future__ import annotations

import logging

from mcp_server.loaders.commcare_metadata import _extract_case_types, _extract_form_definitions
from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


class ConnectMetadataLoader(ConnectBaseLoader):
    """Fetch metadata for a Connect opportunity."""

    def load(self) -> dict:
        org_data = self._fetch_org_data()
        opp_detail = self._fetch_opportunity_detail()
        form_definitions: dict = {}
        case_types: list = []
        try:
            app_structure = self._fetch_app_structure()
            # Real Connect returns {"learn_app": <HQ app JSON|null>, "deliver_app": ...}.
            # Each app is HQ application JSON; reuse the CommCare extractors verbatim.
            apps = [
                a for a in (app_structure.get("deliver_app"), app_structure.get("learn_app")) if a
            ]
            form_definitions = _extract_form_definitions(apps)
            case_types = _extract_case_types(apps)
        except Exception:
            logger.exception(
                "Failed to fetch app_structure for opportunity %s; continuing without form_definitions",
                self.opportunity_id,
            )

        logger.info(
            "Loaded metadata for Connect opportunity %s: %s (%d forms)",
            self.opportunity_id,
            opp_detail.get("name", "unknown"),
            len(form_definitions),
        )
        return {
            "opportunity": opp_detail,
            "organizations": org_data.get("organizations", []),
            "programs": org_data.get("programs", []),
            "all_opportunities": org_data.get("opportunities", []),
            "form_definitions": form_definitions,
            "case_types": case_types,
        }

    def _fetch_org_data(self) -> dict:
        url = f"{self.base_url}/export/opp_org_program_list/"
        return self._get(url).json()

    def _fetch_opportunity_detail(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/"
        return self._get(url).json()

    def _fetch_app_structure(self) -> dict:
        url = f"{self.base_url}/export/opportunity/{self.opportunity_id}/app_structure/"
        return self._get(url, params={"app_type": "both"}).json()
