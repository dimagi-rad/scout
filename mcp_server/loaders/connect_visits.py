"""Visit data loader for CommCare Connect.

Fetches user visit records from the v2 paginated JSON export endpoint
(``/export/opportunity/<id>/user_visits/``), normalizing field types and
renaming the API ``id`` field to ``visit_id`` for the downstream writer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader, stringify

logger = logging.getLogger(__name__)


def _normalize_visit(raw: dict, opportunity_id: int) -> dict:
    """Map a v2 visit record into the shape ``_write_connect_visits`` expects.

    The downstream writer (``mcp_server/services/materializer.py``) inserts
    scalars into TEXT columns and JSON-encodes ``form_json``/``images`` into
    JSONB columns. We coerce scalars to strings via ``stringify`` and pass
    ``form_json``/``images`` through as native dict/list.

    The v1 CSV export omitted ``opportunity_id`` from per-row data in some
    cases; we fall back to the loader's known opportunity_id to preserve the
    historical contract.
    """
    form_json = raw.get("form_json") or {}
    if not isinstance(form_json, dict):
        form_json = {}

    images = raw.get("images") or []
    if not isinstance(images, list):
        images = []

    return {
        "visit_id": stringify(raw.get("id")),
        "opportunity_id": stringify(raw.get("opportunity_id") or opportunity_id),
        "username": stringify(raw.get("username")),
        "deliver_unit": stringify(raw.get("deliver_unit")),
        "entity_id": stringify(raw.get("entity_id")),
        "entity_name": stringify(raw.get("entity_name")),
        "visit_date": stringify(raw.get("visit_date")),
        "status": stringify(raw.get("status")),
        "reason": stringify(raw.get("reason")),
        "location": stringify(raw.get("location")),
        "flagged": stringify(raw.get("flagged")),
        "flag_reason": stringify(raw.get("flag_reason")),
        "form_json": form_json,
        "completed_work": stringify(raw.get("completed_work")),
        "status_modified_date": stringify(raw.get("status_modified_date")),
        "review_status": stringify(raw.get("review_status")),
        "review_created_on": stringify(raw.get("review_created_on")),
        "justification": stringify(raw.get("justification")),
        "date_created": stringify(raw.get("date_created")),
        "completed_work_id": stringify(raw.get("completed_work_id")),
        "deliver_unit_id": stringify(raw.get("deliver_unit_id")),
        "images": images,
    }


class ConnectVisitLoader(ConnectBaseLoader):
    """Fetch and normalize user visit data from Connect (v2 paginated JSON)."""

    def load_pages(self) -> Iterator[list[dict]]:
        total = 0
        for page in self._paginate_export_pages("user_visits/"):
            if not page:
                continue
            normalized = [_normalize_visit(r, self.opportunity_id) for r in page]
            total += len(normalized)
            yield normalized
        logger.info("Fetched %d visits for opportunity %s", total, self.opportunity_id)

    def load(self) -> list[dict]:
        return [row for page in self.load_pages() for row in page]
