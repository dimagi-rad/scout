"""Smoke test: full Connect sync pipeline against live data.

Runs the complete Discover → Load → Transform pipeline for each
configured opportunity ID. Uses the REAL platform database.

If prerequisites are missing (OAuth token, TenantMembership), the test
bootstraps what it can and opens a browser for what it can't.

Configure in tests/smoke/.env:
    SCOUT_BASE_URL=http://localhost:8001
    CONNECT_OPPORTUNITY_IDS=532

Run:
    uv run pytest -m smoke --override-ini="addopts=" \
        -o "DJANGO_SETTINGS_MODULE=config.settings.development" \
        -s --log-cli-level=INFO
"""

from __future__ import annotations

import logging
import time
import webbrowser

import pytest
import requests

logger = logging.getLogger(__name__)

# How long to wait for the user to complete OAuth in the browser
OAUTH_WAIT_TIMEOUT = 120
OAUTH_POLL_INTERVAL = 3


def _get_connect_token(user=None):
    """Find a SocialToken for commcare_connect, optionally filtered by user."""
    from allauth.socialaccount.models import SocialToken

    qs = SocialToken.objects.filter(
        account__provider__startswith="commcare_connect",
    )
    if user:
        qs = qs.filter(account__user=user)
    return qs.select_related("account__user").first()


def _get_or_create_membership(user, opp_id):
    """Get or create a TenantMembership for a Connect opportunity."""
    from apps.users.models import TenantCredential, TenantMembership

    tm, created = TenantMembership.objects.get_or_create(
        user=user,
        provider="commcare_connect",
        tenant_id=str(opp_id),
        defaults={"tenant_name": f"Opportunity {opp_id}"},
    )
    if created:
        TenantCredential.objects.get_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        logger.info("Created TenantMembership for opportunity %s (user: %s)", opp_id, user.email)
    return tm


def _check_scout_running(base_url):
    """Verify Scout is reachable (frontend or backend)."""
    try:
        resp = requests.get(base_url, timeout=5, allow_redirects=True)
        return resp.status_code < 500
    except requests.ConnectionError:
        return False


def _wait_for_oauth(base_url):
    """Open browser to the Django OAuth login URL for Connect.

    Uses the Django backend URL directly (not the Vite proxy) so that the
    OAuth redirect_uri matches what's configured on connect.dimagi.com.
    """
    # Go directly to the allauth login URL on Django — this ensures the
    # redirect_uri uses the Django port (matching the OAuth app config).
    oauth_url = (
        f"{base_url}/accounts/commcare_connect/login/"
        f"?process=connect&next=/api/auth/providers/"
    )

    print()
    print("=" * 70)
    print("  Connect OAuth token not found in the platform database.")
    print()
    print(f"  Opening: {oauth_url}")
    print("  Complete the OAuth flow in your browser.")
    print()
    print("  This test will continue automatically once OAuth completes")
    print(f"  (waiting up to {OAUTH_WAIT_TIMEOUT}s)...")
    print("=" * 70)
    print()

    try:
        webbrowser.open(oauth_url)
    except Exception:
        print(f"  Could not open browser. Visit manually:\n  {oauth_url}")

    deadline = time.time() + OAUTH_WAIT_TIMEOUT
    while time.time() < deadline:
        token = _get_connect_token()
        if token:
            logger.info("OAuth token found for user %s", token.account.user.email)
            return token
        time.sleep(OAUTH_POLL_INTERVAL)

    pytest.fail(
        f"Timed out after {OAUTH_WAIT_TIMEOUT}s waiting for Connect OAuth. "
        f"Visit {oauth_url} and complete the OAuth flow."
    )


@pytest.mark.smoke
@pytest.mark.django_db
class TestConnectSync:
    def _resolve_credential(self, tm):
        """Resolve OAuth credential for a TenantMembership."""
        token = _get_connect_token(user=tm.user)
        if not token:
            pytest.fail(
                f"No SocialToken for user {tm.user.email} with provider commcare_connect."
            )
        return {"type": "oauth", "value": token.token}

    def _ensure_prerequisites(self, opp_id, scout_base_url):
        """Ensure OAuth token + TenantMembership exist, bootstrapping as needed."""
        # Step 1: Do we have a Connect OAuth token for ANY user?
        token = _get_connect_token()
        if not token:
            # Need OAuth — check Scout is running first
            if not _check_scout_running(scout_base_url):
                pytest.fail(
                    f"No Connect OAuth token found and Scout is not running at {scout_base_url}.\n"
                    f"Start Scout first:\n"
                    f"  uv run honcho -f Procfile.dev start\n"
                    f"Then re-run this test."
                )
            token = _wait_for_oauth(scout_base_url)

        user = token.account.user
        logger.info("Using OAuth token for user: %s", user.email)

        # Step 2: Get or create TenantMembership for this opportunity
        tm = _get_or_create_membership(user, opp_id)
        return tm

    def test_full_pipeline(self, connect_opportunity_id, scout_base_url):
        """Run the full Connect sync pipeline for one opportunity."""
        from mcp_server.pipeline_registry import get_registry
        from mcp_server.services.materializer import run_pipeline

        opp_id = connect_opportunity_id

        # ── Ensure prerequisites (OAuth + TenantMembership) ───────────────
        tm = self._ensure_prerequisites(opp_id, scout_base_url)

        # ── Resolve credential ────────────────────────────────────────────
        credential = self._resolve_credential(tm)

        # ── Resolve pipeline ──────────────────────────────────────────────
        pipeline_config = get_registry().get("connect_sync")
        assert pipeline_config is not None, "connect_sync pipeline not found in registry"

        # ── Run pipeline ──────────────────────────────────────────────────
        def on_progress(current: int, total: int, message: str) -> None:
            logger.info("[%d/%d] %s", current, total, message)

        result = run_pipeline(tm, credential, pipeline_config, on_progress)

        # ── Report results ────────────────────────────────────────────────
        assert result["status"] == "completed", f"Pipeline failed: {result}"
        assert result.get("schema"), "No schema in result"

        sources = result.get("sources", {})
        total_rows = 0

        print()
        print("=" * 70)
        print(f"  Connect sync completed for opportunity {opp_id}")
        print(f"  Schema: {result['schema']}")
        print(f"  Run ID: {result.get('run_id')}")
        print()
        for source_name, source_info in sources.items():
            row_count = source_info if isinstance(source_info, int) else source_info.get("rows", 0)
            print(f"    {source_name}: {row_count:,} rows")
            total_rows += row_count
        print()
        print(f"  Total: {total_rows:,} rows across {len(sources)} sources")
        print("=" * 70)
        print()

        assert total_rows > 0, (
            f"Pipeline completed but loaded 0 rows for opportunity {opp_id}. "
            f"Is there data in this opportunity?"
        )
