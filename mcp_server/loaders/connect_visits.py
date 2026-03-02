"""Visit data loader for CommCare Connect.

Fetches user visit records from the Connect CSV export endpoint,
normalizing JSON/Python-repr fields and renaming ``id`` to ``visit_id``.
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000


def _parse_json_field(value: str) -> dict | list:
    """Parse a JSON field that may be JSON or Python repr format."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        logger.debug("form_json is not valid JSON, trying Python literal_eval")
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            logger.warning("form_json could not be parsed as JSON or Python literal: %.200s", value)
            return {}


def _normalize_visit(raw: dict) -> dict:
    return {
        "visit_id": raw.get("id", ""),
        "opportunity_id": raw.get("opportunity_id", ""),
        "username": raw.get("username", ""),
        "deliver_unit": raw.get("deliver_unit", ""),
        "entity_id": raw.get("entity_id", ""),
        "entity_name": raw.get("entity_name", ""),
        "visit_date": raw.get("visit_date", ""),
        "status": raw.get("status", ""),
        "reason": raw.get("reason", ""),
        "location": raw.get("location", ""),
        "flagged": raw.get("flagged", ""),
        "flag_reason": raw.get("flag_reason", ""),
        "form_json": _parse_json_field(raw.get("form_json", "")),
        "completed_work": raw.get("completed_work", ""),
        "status_modified_date": raw.get("status_modified_date", ""),
        "review_status": raw.get("review_status", ""),
        "review_created_on": raw.get("review_created_on", ""),
        "justification": raw.get("justification", ""),
        "date_created": raw.get("date_created", ""),
        "completed_work_id": raw.get("completed_work_id", ""),
        "deliver_unit_id": raw.get("deliver_unit_id", ""),
        "images": _parse_json_field(raw.get("images", "[]")),
    }


class ConnectVisitLoader(ConnectBaseLoader):
    """Fetch and normalize user visit data from Connect."""

    def load_pages(self) -> Iterator[list[dict]]:
        url = self._opp_url("user_visits/")
        rows = self._get_csv(url)
        logger.info("Fetched %d visits for opportunity %s", len(rows), self.opportunity_id)
        normalized = [_normalize_visit(r) for r in rows]
        for i in range(0, len(normalized), PAGE_SIZE):
            yield normalized[i : i + PAGE_SIZE]

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
