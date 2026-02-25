"""Loader for CommCare form submissions.

CommCare forms are complex: a single form submission can create or update multiple
cases. Case blocks may appear at any nesting depth in the form JSON (e.g.
``form.case``, ``form.group.case``, ``form.repeat[0].case``). The loader extracts
all case references from each form and stores them alongside the raw form data.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from mcp_server.loaders.commcare_base import CommCareBaseLoader

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"


class CommCareFormLoader(CommCareBaseLoader):
    """Loads form submission records from the CommCare HQ API.

    Supports both ``load()`` (returns flat list) and ``load_pages()``
    (yields one page at a time for streaming writes).

    Each returned record is a flat dict with:
        form_id, xmlns, received_on, server_modified_on, app_id,
        form_data (raw JSONB),
        case_ids (list of case IDs touched by this form)
    """

    def __init__(self, domain: str, credential: dict[str, str], page_size: int = 1000) -> None:
        super().__init__(domain=domain, credential=credential)
        self.page_size = min(page_size, 1000)

    def load_pages(self) -> Iterator[list[dict]]:
        """Yield one page of normalised form records at a time."""
        url = f"{_BASE_URL}/a/{self.domain}/api/v0.5/form/"
        params: dict = {"limit": self.page_size}
        total_loaded = 0
        while url:
            data = self._get(url, params=params).json()
            forms = [_normalize_form(raw) for raw in data.get("objects", [])]
            if forms:
                total_loaded += len(forms)
                logger.info(
                    "Fetched %d forms (total so far: %d/%s) for domain %s",
                    len(forms),
                    total_loaded,
                    data.get("meta", {}).get("total_count", "?"),
                    self.domain,
                )
                yield forms
            url = data.get("next")
            params = {}

    def load(self) -> list[dict]:
        """Return all forms as a flat list (loads all pages into memory)."""
        return [form for page in self.load_pages() for form in page]


def _normalize_form(raw: dict) -> dict:
    """Flatten a raw CommCare form API response into a loader record."""
    form_data = raw.get("form", {})
    case_refs = extract_case_refs(form_data)
    return {
        "form_id": raw.get("id", ""),
        "xmlns": form_data.get("@xmlns", ""),
        "received_on": raw.get("received_on", ""),
        "server_modified_on": raw.get("server_modified_on", ""),
        "app_id": raw.get("app_id", ""),
        "form_data": form_data,
        "case_ids": [r["case_id"] for r in case_refs],
    }


def extract_case_refs(form_data: Any, _seen: set[str] | None = None) -> list[dict]:
    """Recursively extract all case block references from a form's data dict.

    CommCare case blocks are identified by the presence of ``@case_id`` in a dict.
    They can be nested at any depth and may appear inside repeat groups (lists).

    Returns a deduplicated list of dicts with ``case_id`` and ``action`` keys.
    """
    if _seen is None:
        _seen = set()
    refs: list[dict] = []

    if isinstance(form_data, dict):
        if "@case_id" in form_data:
            case_id = form_data["@case_id"]
            if case_id and case_id not in _seen:
                _seen.add(case_id)
                refs.append(
                    {
                        "case_id": case_id,
                        "action": form_data.get("@action", ""),
                    }
                )
        else:
            for value in form_data.values():
                refs.extend(extract_case_refs(value, _seen))

    elif isinstance(form_data, list):
        for item in form_data:
            refs.extend(extract_case_refs(item, _seen))

    return refs
