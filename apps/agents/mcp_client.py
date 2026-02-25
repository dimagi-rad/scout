"""
MCP client for connecting the Scout agent to the MCP data server.

Creates a fresh MultiServerMCPClient per call so per-request progress
callbacks can be attached. Circuit breaker logic prevents hammering an
unavailable server.
"""

from __future__ import annotations

import logging
import time

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.conf import settings
from langchain_mcp_adapters.callbacks import Callbacks, ProgressCallback
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

# Circuit breaker state
_consecutive_failures: int = 0
_last_failure_time: float = 0.0
_CIRCUIT_BREAKER_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN = 30.0


class MCPServerUnavailable(Exception):
    """Raised when the circuit breaker is open."""


async def get_mcp_tools(on_progress: ProgressCallback | None = None) -> list:
    """Load MCP tools as LangChain tools.

    Creates a fresh MultiServerMCPClient on each call. Pass on_progress to
    receive real-time step updates during long-running tools such as
    run_materialization.

    Raises MCPServerUnavailable when the circuit breaker is open.
    """
    global _consecutive_failures, _last_failure_time

    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        elapsed = time.monotonic() - _last_failure_time
        if elapsed < _CIRCUIT_BREAKER_COOLDOWN:
            raise MCPServerUnavailable(
                f"MCP server circuit breaker open ({_consecutive_failures} consecutive failures). "
                f"Retry in {_CIRCUIT_BREAKER_COOLDOWN - elapsed:.0f}s."
            )
        logger.info("Circuit breaker cooldown elapsed, allowing retry")

    url = settings.MCP_SERVER_URL
    callbacks = Callbacks(on_progress=on_progress) if on_progress else None
    try:
        client = MultiServerMCPClient(
            {"scout-data": {"transport": "streamable_http", "url": url}},
            callbacks=callbacks,
        )
        tools = await client.get_tools()
        logger.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
        _consecutive_failures = 0
        return tools
    except MCPServerUnavailable:
        raise
    except Exception:
        _consecutive_failures += 1
        _last_failure_time = time.monotonic()
        logger.error("MCP tool loading failed (attempt %d)", _consecutive_failures)
        raise


def reset_circuit_breaker() -> None:
    """Reset circuit breaker state. Used in tests."""
    global _consecutive_failures, _last_failure_time
    _consecutive_failures = 0
    _last_failure_time = 0.0


# --- OAuth token retrieval ---

COMMCARE_PROVIDERS = frozenset({"commcare", "commcare_connect"})


async def get_user_oauth_tokens(user) -> dict[str, str]:
    """Retrieve OAuth tokens for a user's CommCare providers."""
    if user is None or not getattr(user, "pk", None):
        return {}
    return await sync_to_async(_get_tokens_sync)(user)


def _get_tokens_sync(user) -> dict[str, str]:
    social_tokens = SocialToken.objects.filter(
        account__user=user,
        account__provider__in=COMMCARE_PROVIDERS,
    ).select_related("account")
    return {
        st.account.provider: st.token
        for st in social_tokens
        if st.account.provider in COMMCARE_PROVIDERS
    }
