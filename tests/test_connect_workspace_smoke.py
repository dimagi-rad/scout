"""
Smoke test: Connect OAuth → workspace selector pipeline.

Verifies the full lifecycle:
  1. User connects CommCare Connect via OAuth
  2. resolve_connect_opportunities() fetches opps from the Connect API
  3. TenantMembership records are persisted in the DB
  4. GET /api/auth/tenants/ returns those memberships
  5. After disconnect, memberships are cleaned up and cache is cleared
  6. After reconnect, resolution runs again and memberships reappear

Each layer is instrumented so failures pinpoint the broken component.
"""

from unittest.mock import MagicMock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.sites.models import Site

from apps.users.models import TenantCredential, TenantMembership


def _make_connect_opportunities(count: int) -> list[dict]:
    """Generate a list of fake Connect opportunities."""
    return [{"id": i, "name": f"Opportunity {i}"} for i in range(1, count + 1)]


@pytest.fixture
def site(db):
    return Site.objects.get_current()


@pytest.fixture
def connect_social_app(db, site):
    app = SocialApp.objects.create(
        provider="commcare_connect",
        name="CommCare Connect",
        client_id="connect-client-id",
        secret="connect-secret",
    )
    app.sites.add(site)
    return app


@pytest.fixture
def connect_social_account(db, user):
    return SocialAccount.objects.create(
        user=user,
        provider="commcare_connect",
        uid="connect-user-123",
        extra_data={"email": user.email},
    )


@pytest.fixture
def connect_social_token(db, connect_social_account, connect_social_app):
    return SocialToken.objects.create(
        app=connect_social_app,
        account=connect_social_account,
        token="test-connect-access-token",
        token_secret="test-connect-refresh-token",
    )


def _mock_connect_api(opportunity_count: int):
    """Return a context manager that patches the Connect API response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "opportunities": _make_connect_opportunities(opportunity_count),
    }
    return patch(
        "apps.users.services.tenant_resolution.requests.get",
        return_value=mock_response,
    )


# ---------------------------------------------------------------------------
# Layer-by-layer diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConnectResolutionPipeline:
    """Layer-by-layer checks for the Connect → workspace selector pipeline."""

    def test_layer1_resolve_creates_memberships(self, user):
        """Layer 1: resolve_connect_opportunities() creates TenantMembership rows."""
        from apps.users.services.tenant_resolution import resolve_connect_opportunities

        with _mock_connect_api(opportunity_count=3):
            memberships = resolve_connect_opportunities(user, "fake-token")

        assert len(memberships) == 3, (
            f"Expected 3 memberships, got {len(memberships)}. "
            "Resolution function did not create TenantMembership records."
        )
        for tm in memberships:
            assert tm.provider == "commcare_connect"
            assert TenantCredential.objects.filter(
                tenant_membership=tm, credential_type=TenantCredential.OAUTH
            ).exists(), f"Missing OAuth credential for membership {tm.tenant_id}"

    def test_layer2_tenant_list_returns_memberships(
        self, client, user, connect_social_token
    ):
        """Layer 2: GET /api/auth/tenants/ returns resolved Connect memberships."""
        client.force_login(user)

        with _mock_connect_api(opportunity_count=5):
            resp = client.get("/api/auth/tenants/")

        assert resp.status_code == 200
        data = resp.json()
        connect_entries = [d for d in data if d["provider"] == "commcare_connect"]
        assert len(connect_entries) == 5, (
            f"Expected 5 Connect entries from /api/auth/tenants/, got {len(connect_entries)}. "
            f"Full response: {data}"
        )

    def test_layer3_signal_triggers_resolution(self, user, connect_social_token):
        """Layer 3: The social_account signal triggers resolve_connect_opportunities."""
        from allauth.socialaccount.signals import social_account_added

        # Simulate the signal that fires after OAuth connect
        mock_sociallogin = MagicMock()
        mock_sociallogin.account.provider = "commcare_connect"
        mock_sociallogin.user = user
        mock_sociallogin.token.token = "fake-token"

        with _mock_connect_api(opportunity_count=2):
            social_account_added.send(
                sender=SocialAccount,
                request=None,
                sociallogin=mock_sociallogin,
            )

        count = TenantMembership.objects.filter(
            user=user, provider="commcare_connect"
        ).count()
        assert count == 2, (
            f"Signal should have created 2 memberships, found {count}. "
            "Check that apps.users.signals.resolve_tenant_on_social_login is connected."
        )


# ---------------------------------------------------------------------------
# Full lifecycle smoke test
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConnectDisconnectReconnectLifecycle:
    """
    End-to-end: connect → verify opps → disconnect → verify cleanup → reconnect → verify opps.
    """

    def test_full_lifecycle(
        self, client, user, connect_social_app, connect_social_account, connect_social_token
    ):
        client.force_login(user)

        # ── Step 1: Initial connect — resolve opportunities ──────────
        with _mock_connect_api(opportunity_count=150):
            resp = client.get("/api/auth/tenants/")
        assert resp.status_code == 200
        data = resp.json()
        connect_entries = [d for d in data if d["provider"] == "commcare_connect"]
        assert len(connect_entries) == 150, (
            f"STEP 1 FAILED: After connect, expected 150 Connect entries, got {len(connect_entries)}."
        )

        # ── Step 2: Disconnect ───────────────────────────────────────
        resp = client.post("/api/auth/providers/commcare_connect/disconnect/")
        assert resp.status_code == 200, (
            f"STEP 2 FAILED: Disconnect returned {resp.status_code}: {resp.json()}"
        )

        # Verify: token deleted
        assert not SocialToken.objects.filter(
            account=connect_social_account
        ).exists(), "STEP 2 FAILED: SocialToken still exists after disconnect."

        # Verify: OAuth-based memberships cleaned up
        oauth_memberships = TenantMembership.objects.filter(
            user=user,
            provider="commcare_connect",
            credential__credential_type=TenantCredential.OAUTH,
        ).count()
        assert oauth_memberships == 0, (
            f"STEP 2 FAILED: Expected 0 OAuth memberships after disconnect, found {oauth_memberships}."
        )

        # Verify: tenant list API reflects the cleanup
        resp = client.get("/api/auth/tenants/")
        assert resp.status_code == 200
        connect_entries = [d for d in resp.json() if d["provider"] == "commcare_connect"]
        assert len(connect_entries) == 0, (
            f"STEP 2 FAILED: /api/auth/tenants/ still returns {len(connect_entries)} Connect entries after disconnect."
        )

        # ── Step 3: Reconnect — create a new token ──────────────────
        new_token = SocialToken.objects.create(
            app=connect_social_app,
            account=connect_social_account,
            token="new-connect-access-token",
            token_secret="new-connect-refresh-token",
        )
        assert new_token.pk, "STEP 3 FAILED: Could not create new SocialToken."

        # ── Step 4: Fetch tenants — should re-resolve ────────────────
        # Use ?refresh=1 to force resolution since the Django-cache TTL
        # may have been set by the verification call in Step 2.
        with _mock_connect_api(opportunity_count=150):
            resp = client.get("/api/auth/tenants/?refresh=1")
        assert resp.status_code == 200
        data = resp.json()
        connect_entries = [d for d in data if d["provider"] == "commcare_connect"]
        assert len(connect_entries) == 150, (
            f"STEP 4 FAILED: After reconnect, expected 150 Connect entries, "
            f"got {len(connect_entries)}. "
            "Resolution did not run after reconnect."
        )

        # ── Step 5: Verify all have OAuth credentials ────────────────
        for entry in connect_entries:
            tm = TenantMembership.objects.get(id=entry["id"])
            assert hasattr(tm, "credential"), (
                f"STEP 5 FAILED: Membership {tm.tenant_id} has no credential."
            )
            assert tm.credential.credential_type == TenantCredential.OAUTH, (
                f"STEP 5 FAILED: Membership {tm.tenant_id} credential is "
                f"{tm.credential.credential_type}, expected oauth."
            )
