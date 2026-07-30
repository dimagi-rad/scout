"""Caller authentication for the MCP HTTP server (arch #253, finding 01#6).

The streamable-HTTP MCP server runs on an internal, IP-filtered network and is
reached only by the Django API and the Procrastinate worker over the Docker
network. Previously it had *no* caller authentication at all: every tenant-scoped
tool resolved context purely from the ``workspace_id`` argument, so any
co-located process, SSRF, or dev port-forward could call destructive tools
(``teardown_schema``) against any workspace. Isolation was network topology only.

We add a lightweight shared-secret check as defense-in-depth (not a perimeter):
every request must carry ``X-Scout-MCP-Secret`` matching ``MCP_SHARED_SECRET``.
The check is a Starlette middleware so it fires before any tool dispatch,
including the MCP session/initialize handshake.

Fail-open when unset: if ``MCP_SHARED_SECRET`` is empty (local dev, where the
server is loopback-only) the check is disabled and a warning is logged once, so
developers are not forced to set a secret. Production deploy configs set it and
the matching clients send it (``apps/agents/mcp_client.py``).
"""

from __future__ import annotations

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Header the MCP clients (Django API, worker) send the shared secret in.
SHARED_SECRET_HEADER = "X-Scout-MCP-Secret"  # noqa: S105 — header name, not a credential


class SharedSecretMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not carry the configured shared secret.

    A constant-time comparison guards against timing oracles. When ``secret`` is
    empty the middleware is a no-op (fail-open) and logs a one-time warning.
    """

    def __init__(self, app, *, secret: str) -> None:
        super().__init__(app)
        self._secret = secret or ""
        if not self._secret:
            logger.warning(
                "MCP_SHARED_SECRET is not set — MCP caller authentication is DISABLED. "
                "Set MCP_SHARED_SECRET in production so only the Scout API/worker can "
                "reach the MCP server."
            )

    async def dispatch(self, request: Request, call_next):
        if self._secret:
            provided = request.headers.get(SHARED_SECRET_HEADER, "")
            if not hmac.compare_digest(provided, self._secret):
                logger.warning(
                    "Rejected MCP request without a valid shared secret (path=%s)",
                    request.url.path,
                )
                return JSONResponse(
                    {"error": "Unauthorized: missing or invalid MCP shared secret"},
                    status_code=401,
                )
        return await call_next(request)
