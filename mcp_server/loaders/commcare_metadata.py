"""Metadata loader for CommCare HQ — discovers app structure, case types, form definitions."""

from __future__ import annotations

import logging

from mcp_server.loaders.commcare_base import CommCareBaseLoader

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"


class CommCareMetadataLoader(CommCareBaseLoader):
    """Discovers tenant metadata from CommCare HQ Application API.

    Returns a plain dict stored directly in TenantMetadata.metadata.
    Structure:
        {
            "app_definitions": [...],    # raw app JSON from CommCare API
            "case_types": [              # unique case types across all apps
                {"name": str, "app_id": str, "app_name": str, "module_name": str}
            ],
            "form_definitions": {        # keyed by xmlns
                "<xmlns>": {"name": str, "app_name": str, "module_name": str, "case_type": str, "questions": [...]}
            },
        }
    """

    def load(self) -> dict:
        apps = self._fetch_apps()
        case_types = _extract_case_types(apps)
        form_definitions = _extract_form_definitions(apps)
        logger.info(
            "Discovered %d apps, %d case types, %d forms for domain %s",
            len(apps),
            len(case_types),
            len(form_definitions),
            self.domain,
        )
        return {
            "app_definitions": apps,
            "case_types": case_types,
            "form_definitions": form_definitions,
        }

    def _fetch_apps(self) -> list[dict]:
        url = f"{_BASE_URL}/a/{self.domain}/api/v0.5/application/"
        params: dict = {"limit": 100}
        apps: list[dict] = []
        while url:
            data = self._get(url, params=params).json()
            apps.extend(data.get("objects", []))
            url = data.get("next")
            params = {}
        return apps


def _extract_case_types(apps: list[dict]) -> list[dict]:
    """Extract unique case types from application module definitions."""
    seen: set[str] = set()
    case_types: list[dict] = []
    for app in apps:
        for module in app.get("modules", []):
            ct = module.get("case_type", "")
            if ct and ct not in seen:
                seen.add(ct)
                case_types.append(
                    {
                        "name": ct,
                        "app_id": app.get("id", ""),
                        "app_name": app.get("name", ""),
                        "module_name": module.get("name", ""),
                    }
                )
    return case_types


def _extract_form_definitions(apps: list[dict]) -> dict[str, dict]:
    """Extract form definitions keyed by form xmlns."""
    forms: dict[str, dict] = {}
    for app in apps:
        for module in app.get("modules", []):
            for form in module.get("forms", []):
                xmlns = form.get("xmlns", "")
                if xmlns:
                    forms[xmlns] = {
                        "name": form.get("name", ""),
                        "app_name": app.get("name", ""),
                        "module_name": module.get("name", ""),
                        "case_type": module.get("case_type", ""),
                        "questions": form.get("questions", []),
                    }
    return forms
