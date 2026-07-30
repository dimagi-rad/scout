"""Shared-secret caller authentication for the MCP HTTP server (arch #253, 01#6).

The streamable-HTTP MCP server previously trusted any caller on the internal
network — every tenant-scoped tool resolved context purely from the
``workspace_id`` argument with no principal check. A co-located process, an SSRF,
or a dev port-forward could call ``teardown_schema(confirm=True, workspace_id=...)``
to destroy or read any workspace's data.

We add defense-in-depth: a shared secret (``MCP_SHARED_SECRET``) sent in the
``X-Scout-MCP-Secret`` header on every request. Wrong/missing secret -> 401. When
the secret is unset (local dev) the check is disabled (fail-open) so dev keeps
working, matching how other Scout secrets degrade.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp_server.auth import SHARED_SECRET_HEADER, SharedSecretMiddleware


def _probe_app() -> Starlette:
    async def ok(_request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])


async def _get(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/mcp", headers=headers or {})


@pytest.mark.asyncio
async def test_request_without_secret_is_rejected():
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="topsecret")
    resp = await _get(app)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_request_with_wrong_secret_is_rejected():
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="topsecret")
    resp = await _get(app, headers={SHARED_SECRET_HEADER: "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_request_with_correct_secret_is_accepted():
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="topsecret")
    resp = await _get(app, headers={SHARED_SECRET_HEADER: "topsecret"})
    assert resp.status_code == 200
    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_unset_secret_disables_check_fail_open():
    """With no configured secret (empty), the middleware lets requests through so
    local dev keeps working. Production deploy configs set the secret."""
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="")
    resp = await _get(app)
    assert resp.status_code == 200
