"""Metadata loader for CommCare HQ — discovers app structure, case types, form definitions."""

from __future__ import annotations

import logging

import requests

from mcp_server.loaders.commcare_base import CommCareBaseLoader

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"


def fetch_app_by_id(
    domain: str,
    app_id: str,
    session: requests.Session | None = None,
    timeout: tuple[int, int] = (10, 60),
) -> dict | None:
    """Fetch a single CommCare HQ application by domain and app_id.

    Uses an unauthenticated GET by default. Returns the app dict on success,
    None on any failure (network error, non-200 status, etc.).
    """
    url = f"{_BASE_URL}/a/{domain}/api/v0.5/application/{app_id}/"
    logger.info("fetch_app_by_id: GET %s (authenticated=%s)", url, session is not None)
    try:
        if session is not None:
            resp = session.get(url, timeout=timeout)
        else:
            resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.warning("Failed to fetch app %s from domain %s", app_id, domain, exc_info=True)
        return None


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
        case_types = extract_case_types(apps)
        form_definitions = extract_form_definitions(apps)
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


def extract_case_types(apps: list[dict]) -> list[dict]:
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


def extract_form_definitions(apps: list[dict]) -> dict[str, dict]:
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
