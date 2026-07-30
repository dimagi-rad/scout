"""
MCP client connecting the Scout agent to the MCP data server.

Tool schemas are static, so the tool list is cached across chat turns rather than
re-fetched per message (arch #253, finding 10#1). Caching the list doesn't pin a
connection — each tool still opens its own session at invocation time. Every call
carries the shared secret header for SharedSecretMiddleware (arch #253, 01#6); a
circuit breaker prevents hammering an unavailable server.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_server.auth import SHARED_SECRET_HEADER

logger = logging.getLogger(__name__)

_consecutive_failures: int = 0
_last_failure_time: float = 0.0
_CIRCUIT_BREAKER_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN = 30.0

# Cached tool list (schemas are static; reset via reset_tools_cache()).
_cached_tools: list | None = None


class MCPServerUnavailable(Exception):
    """Raised when the circuit breaker is open."""


def _build_connection() -> dict:
    """Build the streamable-HTTP connection config, attaching the shared secret."""
    conn: dict = {"transport": "streamable_http", "url": settings.MCP_SERVER_URL}
    secret = getattr(settings, "MCP_SHARED_SECRET", "")
    if secret:
        conn["headers"] = {SHARED_SECRET_HEADER: secret}
    return conn


async def get_mcp_tools() -> list:
    """Load MCP tools as LangChain tools.

    Returns a cached tool list when available (the schemas are static); only the
    first call per process performs the ``tools/list`` round trip.

    Raises MCPServerUnavailable when the circuit breaker is open.
    """
    global _consecutive_failures, _last_failure_time, _cached_tools

    if _cached_tools is not None:
        return _cached_tools

    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        elapsed = time.monotonic() - _last_failure_time
        if elapsed < _CIRCUIT_BREAKER_COOLDOWN:
            raise MCPServerUnavailable(
                f"MCP server circuit breaker open ({_consecutive_failures} consecutive failures). "
                f"Retry in {_CIRCUIT_BREAKER_COOLDOWN - elapsed:.0f}s."
            )
        logger.info("Circuit breaker cooldown elapsed, allowing retry")

    try:
        client = MultiServerMCPClient({"scout-data": _build_connection()})
        tools = await client.get_tools()
        logger.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
        _consecutive_failures = 0
        _cached_tools = tools
        return tools
    except MCPServerUnavailable:
        raise
    except Exception:
        _consecutive_failures += 1
        _last_failure_time = time.monotonic()
        logger.exception("MCP tool loading failed (attempt %d)", _consecutive_failures)
        raise


def reset_circuit_breaker() -> None:
    """Reset circuit breaker state. Used in tests."""
    global _consecutive_failures, _last_failure_time
    _consecutive_failures = 0
    _last_failure_time = 0.0


def reset_tools_cache() -> None:
    """Clear the cached MCP tool list. Used in tests; also lets a process drop a
    stale schema cache if the server's tool surface ever changes."""
    global _cached_tools
    _cached_tools = None
