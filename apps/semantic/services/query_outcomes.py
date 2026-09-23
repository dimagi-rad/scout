"""Typed failures for chat and artifact queries; classification never performs repairs."""

import asyncio
import logging
from types import SimpleNamespace

from apps.artifacts.services.query_state import artifact_query_surface
from mcp_server.envelope import error_response

logger = logging.getLogger(__name__)


def query_error(code, message, *, category, retryable=False, recovery_action=None):
    result = error_response(code, message)
    result["error"].update(category=category, retryable=retryable, recovery_action=recovery_action)
    return result


class QueryReadiness:
    """One lazy inspection of a batch's required members, never a global cache."""

    def __init__(self, workspace, queries):
        self.subject = SimpleNamespace(workspace=workspace, semantic_queries=queries, id=None)
        self._surface = None
        self._inspected = False
        self._lock = asyncio.Lock()

    async def surface(self):
        async with self._lock:
            if not self._inspected:
                try:
                    self._surface = await artifact_query_surface(self.subject)
                except Exception:
                    logger.warning(
                        "Unable to inspect query readiness for workspace %s",
                        self.subject.workspace.id,
                        exc_info=True,
                    )
                self._inspected = True
            return self._surface


async def query_readiness_error(workspace, query, code, message, *, category, readiness=None):
    """Reuse artifact-page dependency/readiness decisions for a query not yet saved.

    The subject has no persisted artifact or recovery history. The existing
    service only reads catalog/publication state; it never loads provider data.
    """
    try:
        surface = await (readiness or QueryReadiness(workspace, [query])).surface()
    except Exception:
        logger.warning(
            "Unable to inspect query readiness for workspace %s", workspace.id, exc_info=True
        )
        return query_error(code, message, category=category)
    if surface is None:
        return query_error(code, message, category=category)
    if surface.get("status") == "model_drift":
        return query_error(code, message, category="missing_model_dependency")
    if not surface.get("queryable"):
        action = surface.get("recovery_action")
        return query_error(
            code,
            message,
            category="data_unavailable",
            recovery_action=action
            if action in {"materialization", "view_rebuild", "semantic_rebuild"}
            else None,
        )
    return query_error(code, message, category=category)
