"""Shared-secret caller authentication for the MCP HTTP server (arch #253, 01#6).

The streamable-HTTP MCP server previously trusted any caller on the internal
network — every tenant-scoped tool resolved context purely from the
``workspace_id`` argument with no principal check. A co-located process, an SSRF,
or a dev port-forward could call ``query(workspace_id=...)``
to read any workspace's data.

We add defense-in-depth: a shared secret (``MCP_SHARED_SECRET``) sent in the
``X-Scout-MCP-Secret`` header on every request. Wrong/missing secret -> 401. An
unset secret rejects everything (#51) instead of disabling the check.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from apps.agents.mcp_client import _build_connection
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
@pytest.mark.parametrize("headers", [None, {SHARED_SECRET_HEADER: ""}])
async def test_unset_secret_rejects_every_request(headers):
    """An empty configured secret must not match an empty or missing header."""
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="")
    resp = await _get(app, headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_secret_header_is_rejected_not_an_error():
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="topsecret")
    resp = await _get(app, headers={SHARED_SECRET_HEADER: "t\u00f6psecret".encode("latin-1")})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_secret_sent_as_utf8_bytes_is_accepted():
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="t\u00f6psecret")
    resp = await _get(app, headers={SHARED_SECRET_HEADER: "t\u00f6psecret".encode()})
    assert resp.status_code == 200


async def _get_as_client(app, client_secret: str):
    """Send a request with the headers the Django-side MCP client would attach."""
    with patch("apps.agents.mcp_client.settings") as mock_settings:
        mock_settings.MCP_SERVER_URL = "http://test/mcp"
        mock_settings.MCP_SHARED_SECRET = client_secret
        conn = _build_connection()
    return await _get(app, headers=conn["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["topsecret", "t\u00f6psecret-\u2603"])
async def test_client_secret_round_trips_to_server(secret):
    """E1: headers arrive latin-1 decoded; a non-ASCII secret must still match."""
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret=secret)
    resp = await _get_as_client(app, secret)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_client_secret_mismatch_is_rejected():
    app = _probe_app()
    app.add_middleware(SharedSecretMiddleware, secret="t\u00f6psecret")
    resp = await _get_as_client(app, "topsecret")
    assert resp.status_code == 401
