"""
MCP client for connecting the Scout agent to the MCP data server.

The MCP tool *schemas* are static, so the tool list is loaded once and cached
across chat turns instead of doing a ``tools/list`` HTTP round trip on every
message (arch #253, finding 10#1). Each cached tool still opens its own MCP
session at invocation time (langchain-mcp-adapters starts a new session per
tool call), so caching the list does not pin a long-lived connection.

Every call carries the shared secret in the ``X-Scout-MCP-Secret`` header so the
MCP server's ``SharedSecretMiddleware`` accepts it (arch #253, finding 01#6).
A circuit breaker prevents hammering an unavailable server.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_server.auth import SHARED_SECRET_HEADER

logger = logging.getLogger(__name__)

# Circuit breaker state
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
