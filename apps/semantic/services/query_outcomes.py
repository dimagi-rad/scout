"""Typed failures for chat and artifact queries; classification never performs repairs."""

import asyncio
import logging

from apps.semantic.services.query_readiness import query_surface_readiness
from mcp_server.envelope import error_response

logger = logging.getLogger(__name__)


def query_error(code, message, *, category, retryable=False, recovery_action=None):
    result = error_response(code, message)
    result["error"].update(category=category, retryable=retryable, recovery_action=recovery_action)
    return result


class QueryReadiness:
    """One lazy inspection of a batch's required members, never a global cache."""

    def __init__(self, workspace, queries):
        self.workspace = workspace
        self.queries = queries
        self._surface = None
        self._inspected = False
        self._lock = asyncio.Lock()

    async def surface(self):
        async with self._lock:
            if not self._inspected:
                try:
                    readiness = await query_surface_readiness(self.workspace, self.queries)
                    self._surface = readiness.surface
                except Exception:
                    logger.warning(
                        "Unable to inspect query readiness for workspace %s",
                        self.workspace.id,
                        exc_info=True,
                    )
                self._inspected = True
            return self._surface


async def query_readiness_error(workspace, query, code, message, *, category, readiness=None):
    """Classify a failed query by the readiness verdict saved artifacts also use.

    An unsaved query has no recovery history; readiness reads only catalog and
    publication state and never loads provider data.
    """
    surface = await (readiness or QueryReadiness(workspace, [query])).surface()
    if surface is None:
        return query_error(code, message, category=category)
    if surface.get("status") == "model_drift":
        # This path handles catalog/build failures, not member-resolution errors.
        # Batch drift cannot prove that this particular query has a missing member.
        return query_error(code, message, category="data_unavailable")
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
