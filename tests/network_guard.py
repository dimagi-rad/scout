"""Fail any test that sends a request to a non-local host.

PR497 shipped two tests that drew a live 401 from CommCare production because
nothing stopped an unstubbed client from reaching the real URL. The guard checks
the request's *target* host at the lowest layer that still knows it, underneath
every mocking library we use (pytest-httpx and requests-mock intercept above it,
so a stubbed call never reaches the guard):

- httpcore's sync and async connection pools, which every httpx client goes
  through. Patching ``socket`` does not work: async httpx connects via
  ``loop.sock_connect`` and anyio captures ``getaddrinfo`` at import time.
- urllib3's ``_make_request`` and, for proxy tunnels, the connection's socket
  creation, which every ``requests`` (and botocore) call goes through. This sits
  below the requests adapter on purpose: the retry tests stub
  ``HTTPSConnectionPool._make_request`` so the real urllib3 ``Retry`` runs, and a
  guard above that layer would flag them.

Checking where the socket connects is not enough either: with ``HTTPS_PROXY`` set
(safe-chain sets it for ``uv run``) the connection goes to a localhost proxy,
which then reaches production on the test's behalf.

A blocked attempt raises ``OutboundNetworkBlocked`` and is also recorded, and the
autouse fixture fails the test at teardown if anything was recorded. The second
half matters because application code often catches broad exceptions: the PR497
tests passed on the transport error of the unstubbed call. Attempts recorded
outside a test (higher-scoped fixtures, threads that outlive their test) fail the
next test, or the session if no test follows.

Out of scope by construction: raw-socket clients such as psycopg and smtplib.
"""

from __future__ import annotations

import ipaddress
import os
import threading
from urllib.parse import urlsplit

import httpcore
import pytest
import urllib3.connection
import urllib3.connectionpool

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104

# Hosts named by the test environment's own service URLs (CI may point these at
# service containers rather than localhost).
_SERVICE_URL_ENV_VARS = (
    "DATABASE_URL",
    "MANAGED_DATABASE_URL",
    "CUBE_API_URL",
    "CUBE_VALIDATOR_URL",
    "MCP_SERVER_URL",
    "REDIS_URL",
)
_SERVICE_HOST_ENV_VARS = ("DATABASE_HOST",)


class OutboundNetworkBlocked(RuntimeError):
    """Raised when a test tries to reach a non-local host."""


# Read on every check: caching would freeze whatever a monkeypatching test had set
# when the first request happened, and .env may only land once settings load.
def _service_hosts() -> set[str]:
    hosts = set()
    for var in _SERVICE_URL_ENV_VARS:
        value = os.environ.get(var)
        if value:
            host = urlsplit(value).hostname
            if host:
                hosts.add(host.lower())
    for var in _SERVICE_HOST_ENV_VARS:
        value = os.environ.get(var)
        if value:
            hosts.add(value.lower())
    return hosts


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Guard:
    def __init__(self) -> None:
        self.enabled = True
        self.violations: list[str] = []
        self._lock = threading.Lock()
        self._originals: list[tuple[object, str, object]] = []

    def is_allowed(self, host: str | bytes | None) -> bool:
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        host = (host or "").strip("[]").lower()
        return (
            not self.enabled
            or host in _LOCAL_HOSTS
            or host in _service_hosts()
            or host.endswith(".localhost")
            or _is_loopback(host)
        )

    def check(self, host: str | bytes | None, port: int | None, via: str) -> None:
        if self.is_allowed(host):
            return
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        target = f"{host}:{port}" if port else host
        message = (
            f"Test attempted a real outbound request to {target} via {via}. "
            "Stub it (httpx_mock / requests_mock / mock.patch), or mark the test "
            "@pytest.mark.allow_network if it genuinely needs the network."
        )
        with self._lock:
            self.violations.append(message)
        raise OutboundNetworkBlocked(message)

    def drain(self) -> list[str]:
        with self._lock:
            violations, self.violations = self.violations, []
        return violations

    def _patch(self, owner: object, name: str, replacement: object) -> None:
        self._originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def install(self) -> None:
        if self._originals:
            return
        guard = self

        pool_request = httpcore.ConnectionPool.handle_request

        def guarded_pool_request(pool, request):
            guard.check(request.url.host, request.url.port, "httpx (sync)")
            return pool_request(pool, request)

        async_pool_request = httpcore.AsyncConnectionPool.handle_async_request

        async def guarded_async_pool_request(pool, request):
            guard.check(request.url.host, request.url.port, "httpx (async)")
            return await async_pool_request(pool, request)

        make_request = urllib3.connectionpool.HTTPConnectionPool._make_request

        def guarded_make_request(pool, conn, method, url, *args, **kwargs):
            # Through a forwarding proxy the pool is the proxy's and the URL is
            # absolute; otherwise it is a path whose query may itself contain "://".
            target = urlsplit(url) if url.startswith(("http://", "https://")) else None
            host = (target.hostname if target else None) or pool.host
            port = target.port if target else pool.port
            guard.check(host, port, "requests/urllib3")
            return make_request(pool, conn, method, url, *args, **kwargs)

        new_conn = urllib3.connection.HTTPConnection._new_conn

        def guarded_new_conn(conn):
            # A CONNECT tunnel reaches the target before any request is made.
            if conn._tunnel_host:
                guard.check(conn._tunnel_host, conn._tunnel_port, "requests/urllib3 (proxy)")
            return new_conn(conn)

        self._patch(httpcore.ConnectionPool, "handle_request", guarded_pool_request)
        self._patch(
            httpcore.AsyncConnectionPool, "handle_async_request", guarded_async_pool_request
        )
        self._patch(
            urllib3.connectionpool.HTTPConnectionPool, "_make_request", guarded_make_request
        )
        self._patch(urllib3.connection.HTTPConnection, "_new_conn", guarded_new_conn)

    def uninstall(self) -> None:
        while self._originals:
            owner, name, original = self._originals.pop()
            setattr(owner, name, original)


guard = _Guard()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: let this test open real outbound connections (smoke tests "
        "get this implicitly)",
    )
    guard.install()


def pytest_unconfigure(config):
    guard.uninstall()


def _opted_out(item) -> bool:
    return bool(item.get_closest_marker("allow_network") or item.get_closest_marker("smoke"))


def _report(violations: list[str]) -> str:
    return "\n".join(dict.fromkeys(violations))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # A hook rather than the fixture so the opt-out also covers class- and
    # session-scoped fixtures, which are set up before any function fixture.
    guard.enabled = not _opted_out(item)
    leftovers = guard.drain()
    if leftovers:
        pytest.fail(
            "Blocked outbound request(s) recorded before this test started "
            "(a higher-scoped fixture, or a thread from an earlier test):\n" + _report(leftovers),
            pytrace=False,
        )


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    guard.enabled = True


def pytest_sessionfinish(session, exitstatus):
    leftovers = guard.drain()
    if leftovers:
        session.config.get_terminal_writer().line(
            "Blocked outbound request(s) recorded after the last test:\n" + _report(leftovers),
            red=True,
        )
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _block_outbound_network():
    yield
    violations = guard.drain()
    if violations:
        pytest.fail(_report(violations), pytrace=False)
