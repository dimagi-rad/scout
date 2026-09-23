"""Typed failures for chat and artifact queries; classification never performs repairs."""

import logging
from types import SimpleNamespace

from apps.artifacts.services.query_state import artifact_query_surface
from mcp_server.envelope import error_response

logger = logging.getLogger(__name__)


def query_error(code, message, *, category, retryable=False, recovery_action=None):
    result = error_response(code, message)
    result["error"].update(category=category, retryable=retryable, recovery_action=recovery_action)
    return result


async def query_readiness_error(workspace, query, code, message, *, category):
    """Reuse artifact-page dependency/readiness decisions for a query not yet saved.

    The subject has no persisted artifact or recovery history. The existing
    service only reads catalog/publication state; it never loads provider data.
    """
    subject = SimpleNamespace(workspace=workspace, semantic_queries=[query], id=None)
    try:
        surface = await artifact_query_surface(subject)
    except Exception:
        logger.warning(
            "Unable to inspect query readiness for workspace %s", workspace.id, exc_info=True
        )
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
