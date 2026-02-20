"""
Response envelope helpers for the MCP server.

Every tool response is wrapped in a consistent envelope:

    Success: {"success": True, "data": {...}, "tenant_id": "...", ...}
    Error:   {"success": False, "error": {"code": "...", "message": "..."}}

Also provides timing, error classification, and structured audit logging.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Audit logger — separate from the module logger so it can be filtered/routed
audit_logger = logging.getLogger("mcp_server.audit")

# --- Error codes ---

VALIDATION_ERROR = "VALIDATION_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"
QUERY_TIMEOUT = "QUERY_TIMEOUT"
NOT_FOUND = "NOT_FOUND"
INTERNAL_ERROR = "INTERNAL_ERROR"
AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"


def success_response(
    data: dict[str, Any],
    *,
    tenant_id: str = "",
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
    if tenant_id:
        envelope["tenant_id"] = tenant_id
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


# Fields that must never appear in audit logs
_SCRUB_KEYS = frozenset({"oauth_tokens"})


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
        context_id: tenant_id or project_id for logging.
    """
    timer = Timer()
    tc: dict[str, Any] = {"timer": timer}
    try:
        yield tc
    finally:
        status = "success" if tc.get("result", {}).get("success") else "error"
        audit_logger.info(
            "tool_call tool=%s context_id=%s status=%s timing_ms=%d %s",
            tool_name,
            context_id,
            status,
            timer.elapsed_ms,
            " ".join(f"{k}={v!r}" for k, v in scrub_extra_fields(extra_fields).items())
            if extra_fields
            else "",
        )
