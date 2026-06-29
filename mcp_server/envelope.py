"""
Response envelope helpers for the MCP server.

Every tool response is wrapped in a consistent envelope:

    Success: {"success": True, "data": {...}, "schema": "...", ...}
    Error:   {"success": False, "error": {"code": "...", "message": "..."}}

Also provides timing, error classification, and structured audit logging.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections

logger = logging.getLogger(__name__)

# Dead-DB-connection hygiene for the long-lived MCP server process (arch #253,
# finding 08#0). The MCP server is a Django-ORM process with no HTTP
# request_started/request_finished cycle, so a connection that dies underneath
# it (RDS restart/upgrade, idle TCP timeout) is reused — closed — forever, and
# every ORM-touching tool call fails until the process restarts. Mirroring the
# procrastinate worker's fix (config/procrastinate.py), we close stale/dead
# connections around every tool call so the next ORM use re-opens them. The
# cleanup must run on the asgiref thread-sensitive executor — the same thread
# the async ORM routes queries through — so it reaches the right connection.
_aclose_old_connections = sync_to_async(close_old_connections, thread_sensitive=True)

# Audit logger — separate from the module logger so it can be filtered/routed
audit_logger = logging.getLogger("mcp_server.audit")

VALIDATION_ERROR = "VALIDATION_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"
QUERY_TIMEOUT = "QUERY_TIMEOUT"
NOT_FOUND = "NOT_FOUND"
INTERNAL_ERROR = "INTERNAL_ERROR"
SCHEMA_BUILD_FAILED = "SCHEMA_BUILD_FAILED"
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"  # noqa: S105 — error code constant, not a credential


def success_response(
    data: dict[str, Any],
    *,
    project_id: str = "",
    schema: str,
    timing_ms: int | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap a successful result in the standard envelope."""
    envelope: dict[str, Any] = {
        "success": True,
        "data": data,
        "schema": schema,
    }
    if project_id:
        envelope["project_id"] = project_id
    if warnings:
        envelope["warnings"] = warnings
    if timing_ms is not None:
        envelope["timing_ms"] = timing_ms
    return envelope


def error_response(
    code: str,
    message: str,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build an error envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if detail:
        error["detail"] = detail
    return {"success": False, "error": error}


class Timer:
    """Simple wall-clock timer that returns elapsed milliseconds."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


# Fields that must never appear in audit logs. Currently empty: the only entry
# was ``oauth_tokens``, removed with the dead OAuth-into-MCP transport (arch
# #253, finding 01#0). Kept as the single hook to scrub any sensitive extra
# field a future tool might pass into ``tool_context``.
_SCRUB_KEYS: frozenset[str] = frozenset()


def scrub_extra_fields(extra: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive fields from audit log extra_fields."""
    return {k: v for k, v in extra.items() if k not in _SCRUB_KEYS}


@asynccontextmanager
async def tool_context(tool_name: str, context_id: str, **extra_fields: Any):
    """Context manager that times a tool call and logs an audit record.

    Yields a dict that the caller can populate with ``result`` or ``error``.
    On exit it emits a structured audit log line.

    Args:
        tool_name: Name of the tool being called.
        context_id: The workspace_id (or run_id) the call is scoped to.
        **extra_fields: Per-tool context recorded in the audit line. Pass
            ``user_id`` and ``thread_id`` (injected server-side by the agent
            graph) so the audit trail attributes the call to an actor and a
            conversation, not just a workspace (arch #257, finding 08#8). Empty
            values are dropped so context-free/operator calls stay quiet.
    """
    timer = Timer()
    tc: dict[str, Any] = {"timer": timer}
    # Bracket the body with connection cleanup; see module-level note on dead-DB hygiene.
    await _aclose_old_connections()
    try:
        yield tc
    finally:
        await _aclose_old_connections()
        status = "success" if tc.get("result", {}).get("success") else "error"
        fields = {k: v for k, v in scrub_extra_fields(extra_fields).items() if v not in ("", None)}
        audit_logger.info(
            "tool_call tool=%s context_id=%s status=%s timing_ms=%d %s",
            tool_name,
            context_id,
            status,
            timer.elapsed_ms,
            " ".join(f"{k}={v!r}" for k, v in fields.items()) if fields else "",
        )
