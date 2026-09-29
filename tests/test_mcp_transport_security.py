"""DNS-rebinding Host allowlist for the streamable-HTTP MCP server.

The full Docker Compose stack points the API at ``http://mcp-server:8100/mcp``;
if that Compose service name isn't allowed, every agent tool call gets a 421.
"""

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import Request

from mcp_server.server import TRANSPORT_SECURITY


def _request(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp",
            "headers": [(b"host", host.encode())],
        }
    )


async def _validate(host: str):
    return await TransportSecurityMiddleware(TRANSPORT_SECURITY).validate_request(_request(host))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "mcp-server",
        "mcp-server:8100",
        "localhost:8100",
        "127.0.0.1:8100",
        "scout-mcp-web:8100",
        "scout-staging-mcp-web:8100",
    ],
)
async def test_internal_hosts_are_accepted(host):
    assert await _validate(host) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["evil.example.com", "mcp-server.evil.example.com:8100"])
async def test_other_hosts_are_rejected(host):
    response = await _validate(host)
    assert response is not None
    assert response.status_code == 421
