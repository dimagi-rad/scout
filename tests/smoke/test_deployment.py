"""Deployment smoke tests — verify Scout works when deployed to a server.

These tests make HTTP requests against a running Scout instance to verify
the frontend, API, and routing all work correctly. They do NOT require
Django test infrastructure — they're pure HTTP checks.

Configure via env vars or tests/smoke/.env:
    SCOUT_FRONTEND_URL=https://labs.connect.dimagi.com/scout
    SCOUT_API_URL=https://labs.connect.dimagi.com/scout

Run:
    uv run pytest tests/smoke/test_deployment.py -v -s --override-ini="addopts=" -p no:django
"""

from __future__ import annotations

import pathlib
import re

import environ
import pytest
import requests

# Load smoke .env directly (don't depend on conftest to avoid import issues
# when running with -p no:django)
_smoke_dir = pathlib.Path(__file__).parent
_env_file = _smoke_dir / ".env"
smoke_env = environ.Env()
if _env_file.exists():
    smoke_env.read_env(str(_env_file))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def frontend_url():
    url = smoke_env("SCOUT_FRONTEND_URL", default="http://localhost:3000")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def api_url():
    url = smoke_env("SCOUT_API_URL", default="http://localhost:8000")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def session():
    return requests.Session()


# ── Health & Reachability ────────────────────────────────────────────────────


@pytest.mark.smoke
class TestHealthCheck:
    def test_api_health_endpoint(self, api_url, session):
        resp = session.get(f"{api_url}/health/", timeout=10)
        assert resp.status_code == 200, f"Health check failed: {resp.status_code}"

    def test_frontend_serves_html(self, frontend_url, session):
        resp = session.get(f"{frontend_url}/", timeout=10)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# ── Frontend Assets ──────────────────────────────────────────────────────────


@pytest.mark.smoke
class TestFrontendAssets:
    def test_index_html_references_correct_base_path(self, frontend_url, session):
        resp = session.get(f"{frontend_url}/", timeout=10)
        assert resp.status_code == 200
        html = resp.text
        assert "<script" in html, "No <script> tags in index.html"

        from urllib.parse import urlparse
        path = urlparse(frontend_url).path
        if path and path != "/":
            assert f'src="{path}' in html or f"src='{path}" in html or f'href="{path}' in html, (
                f"Assets don't reference base path '{path}'. "
                f"VITE_BASE_PATH may not be set during build."
            )

    def test_js_assets_load(self, frontend_url, session):
        resp = session.get(f"{frontend_url}/", timeout=10)
        assert resp.status_code == 200
        js_urls = re.findall(r'src="([^"]+\.js)"', resp.text)
        assert js_urls, "No JS assets found in index.html"

        for js_url in js_urls:
            if js_url.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(frontend_url)
                asset_url = f"{parsed.scheme}://{parsed.netloc}{js_url}"
            elif js_url.startswith("http"):
                asset_url = js_url
            else:
                asset_url = f"{frontend_url}/{js_url}"
            asset_resp = session.get(asset_url, timeout=10)
            assert asset_resp.status_code == 200, f"JS asset {js_url} returned {asset_resp.status_code}"

    def test_css_assets_load(self, frontend_url, session):
        resp = session.get(f"{frontend_url}/", timeout=10)
        assert resp.status_code == 200
        css_urls = re.findall(r'href="([^"]+\.css)"', resp.text)
        if not css_urls:
            pytest.skip("No separate CSS files (may be inlined)")
        for css_url in css_urls:
            if css_url.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(frontend_url)
                asset_url = f"{parsed.scheme}://{parsed.netloc}{css_url}"
            else:
                asset_url = css_url
            asset_resp = session.get(asset_url, timeout=10)
            assert asset_resp.status_code == 200, f"CSS asset {css_url} returned {asset_resp.status_code}"


# ── SPA Routing ──────────────────────────────────────────────────────────────


@pytest.mark.smoke
class TestSPARouting:
    SPA_ROUTES = ["/", "/chat", "/artifacts", "/knowledge", "/recipes",
                  "/data-dictionary", "/settings/connections", "/workspaces"]

    @pytest.mark.parametrize("route", SPA_ROUTES)
    def test_spa_route_returns_index_html(self, frontend_url, session, route):
        url = f"{frontend_url}{route}"
        resp = session.get(url, timeout=10, allow_redirects=True)
        assert resp.status_code == 200, f"Route {route} returned {resp.status_code}"
        assert "text/html" in resp.headers.get("content-type", "")


# ── API Endpoints ────────────────────────────────────────────────────────────


@pytest.mark.smoke
class TestAPIEndpoints:
    def test_csrf_endpoint(self, api_url, session):
        resp = session.get(f"{api_url}/api/auth/csrf/", timeout=10)
        assert resp.status_code == 200
        assert "csrfToken" in resp.json()

    def test_me_returns_401_when_unauthenticated(self, api_url):
        fresh = requests.Session()
        resp = fresh.get(f"{api_url}/api/auth/me/", timeout=10)
        assert resp.status_code == 401

    def test_providers_endpoint(self, api_url, session):
        resp = session.get(f"{api_url}/api/auth/providers/", timeout=10)
        assert resp.status_code == 200
        assert "providers" in resp.json()

    def test_workspaces_requires_auth(self, api_url):
        fresh = requests.Session()
        resp = fresh.get(f"{api_url}/api/workspaces/", timeout=10)
        assert resp.status_code in (401, 403)

    def test_oauth_login_urls_include_base_path(self, api_url, session):
        """OAuth provider login_url should include the sub-path prefix."""
        resp = session.get(f"{api_url}/api/auth/providers/", timeout=10)
        assert resp.status_code == 200
        from urllib.parse import urlparse
        base_path = urlparse(api_url).path
        if not base_path or base_path == "/":
            pytest.skip("No sub-path deployment to test")
        providers = resp.json()["providers"]
        for p in providers:
            login_url = p.get("login_url", "")
            assert login_url.startswith(base_path), (
                f"Provider {p['id']} login_url '{login_url}' doesn't include "
                f"base path '{base_path}' — OAuth callbacks will 404"
            )


# ── Authenticated Flow ───────────────────────────────────────────────────────


@pytest.mark.smoke
class TestAuthenticatedFlow:
    @pytest.fixture(scope="class")
    def auth_session(self, api_url):
        email = smoke_env("SCOUT_TEST_EMAIL", default="")
        password = smoke_env("SCOUT_TEST_PASSWORD", default="")
        if not email or not password:
            pytest.skip("SCOUT_TEST_EMAIL/SCOUT_TEST_PASSWORD not set")

        s = requests.Session()
        csrf_resp = s.get(f"{api_url}/api/auth/csrf/", timeout=10)
        csrf_token = csrf_resp.json().get("csrfToken", "")
        resp = s.post(
            f"{api_url}/api/auth/login/",
            json={"email": email, "password": password},
            headers={"X-CSRFToken": csrf_token, "Referer": f"{api_url}/"},
            timeout=10,
        )
        assert resp.status_code == 200, f"Login failed: {resp.status_code} {resp.text}"
        return s

    def test_me_returns_user_info(self, api_url, auth_session):
        resp = auth_session.get(f"{api_url}/api/auth/me/", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "email" in data
        assert "onboarding_complete" in data

    def test_workspace_list_returns_workspaces(self, api_url, auth_session):
        resp = auth_session.get(f"{api_url}/api/workspaces/", timeout=10)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_providers_show_connection_status(self, api_url, auth_session):
        resp = auth_session.get(f"{api_url}/api/auth/providers/", timeout=10)
        assert resp.status_code == 200
        for p in resp.json()["providers"]:
            assert "connected" in p
