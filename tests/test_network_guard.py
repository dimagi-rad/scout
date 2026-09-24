"""The outbound-network guard blocks every client the codebase uses, at the layer
where connections are opened, and leaves stubbed and local traffic alone."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import requests

from tests.network_guard import OutboundNetworkBlocked, _service_hosts, guard

# A reserved TLD guarantees nothing real is reached if the guard ever regresses.
BLOCKED_URL = "https://guard-check.invalid/oauth/token/"


@pytest.fixture
def expect_blocked(_block_outbound_network):
    # Requesting the guard fixture makes this one tear down first, so it drains
    # the expected violation before the guard checks for unexpected ones.
    yield
    violations = guard.drain()
    assert violations, "the guard did not record the blocked attempt"
    assert all("guard-check.invalid" in v for v in violations)


@pytest.fixture
def local_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def test_blocks_sync_httpx(expect_blocked):
    with pytest.raises(OutboundNetworkBlocked, match="via httpx \\(sync\\)"):
        httpx.get(BLOCKED_URL)


@pytest.mark.asyncio
async def test_blocks_async_httpx(expect_blocked):
    async with httpx.AsyncClient() as client:
        with pytest.raises(OutboundNetworkBlocked, match="via httpx \\(async\\)"):
            await client.get(BLOCKED_URL)


def test_blocks_requests(expect_blocked):
    with pytest.raises(OutboundNetworkBlocked, match="via requests/urllib3"):
        requests.get(BLOCKED_URL, timeout=1)


def test_records_attempts_that_application_code_swallows(expect_blocked):
    try:
        httpx.post(BLOCKED_URL)
    except Exception:
        pass


def test_blocks_attempts_from_worker_threads(expect_blocked):
    errors = []

    def call():
        try:
            requests.get(BLOCKED_URL, timeout=1)
        except OutboundNetworkBlocked as exc:
            errors.append(exc)

    worker = threading.Thread(target=call)
    worker.start()
    worker.join()
    assert len(errors) == 1


def test_allows_localhost(local_server):
    assert httpx.get(local_server).status_code == 204
    assert requests.get(local_server, timeout=5).status_code == 204


@pytest.mark.asyncio
async def test_allows_async_localhost(local_server):
    async with httpx.AsyncClient() as client:
        assert (await client.get(local_server)).status_code == 204


def test_stubbed_httpx_never_reaches_the_guard(httpx_mock):
    httpx_mock.add_response(url=BLOCKED_URL, status_code=200, json={"ok": True})
    assert httpx.get(BLOCKED_URL).json() == {"ok": True}


def test_stubbed_requests_never_reaches_the_guard(requests_mock):
    requests_mock.get(BLOCKED_URL, json={"ok": True})
    assert requests.get(BLOCKED_URL, timeout=1).json() == {"ok": True}


@pytest.mark.allow_network
def test_allow_network_marker_disables_the_guard(monkeypatch):
    def refuse(*args, **kwargs):
        raise socket.gaierror("not resolving a real host in a test")

    # Stop at name resolution so the test proves the guard stood aside without
    # actually resolving or dialing anything.
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    with pytest.raises(requests.ConnectionError):
        requests.get(BLOCKED_URL, timeout=1)
    assert guard.drain() == []


def test_service_hosts_come_from_the_test_environment(monkeypatch):
    monkeypatch.setenv("CUBE_API_URL", "http://cube:4000/cubejs-api/v1")
    monkeypatch.setenv("DATABASE_HOST", "Postgres")
    monkeypatch.setenv("MCP_SERVER_URL", "")
    hosts = _service_hosts()
    assert {"cube", "postgres"} <= hosts
    assert "" not in hosts


def test_local_request_whose_query_carries_a_url_is_allowed(local_server):
    url = f"{local_server}?next=https://www.commcarehq.org/a/"
    assert requests.get(url, timeout=5).status_code == 204


def test_allows_loopback_hosts():
    assert guard.is_allowed("localhost")
    assert guard.is_allowed("127.0.0.2")
    assert guard.is_allowed(b"::1")
    assert not guard.is_allowed("127.example.com")
    assert not guard.is_allowed("www.commcarehq.org")
