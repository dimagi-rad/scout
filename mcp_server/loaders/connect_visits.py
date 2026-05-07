"""Visit data loader for CommCare Connect.

Fetches user visit records from the v2 paginated JSON export endpoint
(``/export/opportunity/<id>/user_visits/``), renames the API ``id`` field
to ``visit_id`` for the downstream writer, and passes scalar values
through as native Python types (the writer's typed columns accept them
directly — see ``mcp_server/services/materializer.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.connect_base import ConnectBaseLoader

logger = logging.getLogger(__name__)


def _normalize_visit(raw: dict, opportunity_id: int) -> dict:
    """Map a v2 visit record into the shape ``_write_connect_visits`` expects.

    The v2 JSON returns real Python types (int, bool, list, dict). The
    only reshaping required is:
      1. Rename ``id`` → ``visit_id`` to match the historical downstream
         column name (kept for compatibility with queries and the chat
         agent's mental model of the schema).
      2. Fall back to the loader's ``opportunity_id`` when the per-row
         serializer omits the field (some Connect serializers do).
      3. Defensive-default ``form_json`` and ``images`` to ``{}``/``[]``
         if they arrive as ``None`` or (unexpectedly) non-object types —
         the writer calls ``json.dumps()`` on them.
    Everything else flows through as-is.
    """
    form_json = raw.get("form_json") or {}
    if not isinstance(form_json, dict):
        form_json = {}

    images = raw.get("images") or []
    if not isinstance(images, list):
        images = []

    return {
        "visit_id": raw.get("id"),
        "opportunity_id": raw.get("opportunity_id") or opportunity_id,
        "username": raw.get("username"),
        "deliver_unit": raw.get("deliver_unit"),
        "entity_id": raw.get("entity_id"),
        "entity_name": raw.get("entity_name"),
        "visit_date": raw.get("visit_date"),
        "status": raw.get("status"),
        "reason": raw.get("reason"),
        "location": raw.get("location"),
        "flagged": raw.get("flagged"),
        "flag_reason": raw.get("flag_reason"),
        "form_json": form_json,
        "completed_work": raw.get("completed_work"),
        "status_modified_date": raw.get("status_modified_date"),
        "review_status": raw.get("review_status"),
        "review_created_on": raw.get("review_created_on"),
        "justification": raw.get("justification"),
        "date_created": raw.get("date_created"),
        "completed_work_id": raw.get("completed_work_id"),
        "deliver_unit_id": raw.get("deliver_unit_id"),
        "images": images,
    }


class ConnectVisitLoader(ConnectBaseLoader):
    """Fetch and normalize user visit data from Connect (v2 paginated JSON)."""

    def load_pages(self) -> Iterator[tuple[list[dict], int | None]]:
        total = 0
        for page, page_total in self._paginate_export_pages("user_visits/"):
            if not page:
                continue
            normalized = [_normalize_visit(r, self.opportunity_id) for r in page]
            total += len(normalized)
            yield normalized, page_total
        logger.info("Fetched %d visits for opportunity %s", total, self.opportunity_id)

    def load(self) -> list[dict]:
        return [row for page, _ in self.load_pages() for row in page]
