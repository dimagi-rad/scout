"""Test that credential resolution refreshes expired OAuth tokens."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.services.token_refresh import (
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshUnavailable,
)


@pytest.mark.django_db
class TestCredentialResolverTokenRefresh:
    @pytest.mark.asyncio
    async def test_expired_token_is_refreshed(self):
        from apps.users.services.credential_resolver import _aresolve_oauth_credential

        mock_token = MagicMock()
        mock_token.token = "old-expired-token"
        mock_token.token_secret = "refresh-token"
        mock_token.expires_at = timezone.now() - timedelta(hours=1)
        mock_token.app = MagicMock()

        with (
            patch(
                "apps.users.services.credential_resolver.token_needs_refresh", return_value=True
            ) as mock_needs,
            patch(
                "apps.users.services.credential_resolver.refresh_oauth_token",
                new_callable=AsyncMock,
                return_value="new-fresh-token",
            ) as mock_refresh,
        ):
            result = await _aresolve_oauth_credential(mock_token, "commcare")
            # can_refresh is passed so an unknown expiry only forces a refresh
            # when there is a refresh token to attempt it with (#373).
            mock_needs.assert_called_once_with(mock_token.expires_at, can_refresh=True)
            mock_refresh.assert_awaited_once()
            assert result["value"] == "new-fresh-token"

    @pytest.mark.asyncio
    async def test_valid_token_not_refreshed(self):
        from apps.users.services.credential_resolver import _aresolve_oauth_credential

        mock_token = MagicMock()
        mock_token.token = "still-valid-token"
        mock_token.expires_at = timezone.now() + timedelta(hours=1)

        with (
            patch(
                "apps.users.services.credential_resolver.token_needs_refresh", return_value=False
            ),
            patch(
                "apps.users.services.credential_resolver.refresh_oauth_token",
                new_callable=AsyncMock,
            ) as mock_refresh,
        ):
            result = await _aresolve_oauth_credential(mock_token, "commcare")
            mock_refresh.assert_not_awaited()
            assert result["value"] == "still-valid-token"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_type,code",
        [
            (TokenRefreshError, ErrorCode.AUTH_REFRESH_FAILED),
            (TokenRefreshUnavailable, ErrorCode.AUTH_REFRESH_FAILED),
            (TokenRefreshRejected, ErrorCode.AUTH_TOKEN_EXPIRED),
        ],
    )
    async def test_refresh_failure_fails_closed(self, error_type, code):
        """arch #252 (14#4): a refresh failure at/near expiry must fail closed
        with actionable reconnect guidance, not fall back to a stale token."""
        from apps.users.services.credential_resolver import (
            CredentialResolutionError,
            _aresolve_oauth_credential,
        )

        mock_token = MagicMock()
        mock_token.token = "known-stale-token"
        mock_token.expires_at = timezone.now() - timedelta(minutes=1)

        with (
            patch("apps.users.services.credential_resolver.token_needs_refresh", return_value=True),
            patch(
                "apps.users.services.credential_resolver.refresh_oauth_token",
                new_callable=AsyncMock,
                side_effect=error_type("fail"),
            ),
            pytest.raises(CredentialResolutionError) as exc,
        ):
            await _aresolve_oauth_credential(mock_token, "commcare")
        assert exc.value.code == code
        assert ("reconnect" in exc.value.message.lower()) == (code == ErrorCode.AUTH_TOKEN_EXPIRED)

    @pytest.mark.asyncio
    async def test_near_expiry_with_no_refresh_capability_fails_closed(self):
        """No refresh token / no token URL and the token is near expiry: fail
        closed rather than provisioning a doomed run (arch #252, 14#4)."""
        from apps.users.services.credential_resolver import (
            CredentialResolutionError,
            _aresolve_oauth_credential,
        )

        mock_token = MagicMock()
        mock_token.token = "near-expiry"
        mock_token.token_secret = ""  # no refresh token
        mock_token.expires_at = timezone.now() - timedelta(minutes=1)

        with patch(
            "apps.users.services.credential_resolver.token_needs_refresh", return_value=True
        ):
            with pytest.raises(CredentialResolutionError):
                await _aresolve_oauth_credential(mock_token, "commcare")

    @pytest.mark.asyncio
    async def test_valid_oauth_credential_carries_refresh_callable(self):
        """A refreshable OAuth credential exposes a ``refresh`` callable so
        loaders can renew the token mid-run (arch #252, finding 14#3)."""
        from apps.users.services.credential_resolver import _aresolve_oauth_credential

        mock_token = MagicMock()
        mock_token.token = "valid"
        mock_token.token_secret = "refresh-token"
        mock_token.app = MagicMock()
        mock_token.expires_at = timezone.now() + timedelta(hours=1)

        with patch(
            "apps.users.services.credential_resolver.token_needs_refresh", return_value=False
        ):
            result = await _aresolve_oauth_credential(mock_token, "commcare")
        assert result["value"] == "valid"
        assert callable(result["refresh"])


class TestSyncTokenRefresh:
    @pytest.fixture(autouse=True)
    def _mock_connection_health_storage(self, mocker):
        preflight = mocker.patch("apps.users.services.token_refresh._preflight_token").return_value
        preflight.refresh_token = "old-refresh"
        preflight.expires_at = None
        mocker.patch("apps.users.services.token_refresh._persist_refresh_failure")

    def test_refresh_oauth_token_sync_updates_and_persists(self):
        from apps.users.services.token_refresh import (
            PersistedTokenSnapshot,
            TokenRefreshResult,
            TokenRefreshStatus,
            refresh_oauth_token_sync,
        )

        social_token = MagicMock(token="old-access", token_secret="refresh", app_id=1, account_id=1)
        social_token.token_secret = "old-refresh"
        social_token.app.client_id = "cid"
        social_token.app.secret = "secret"

        response = MagicMock()
        response.json.return_value = {
            "access_token": "brand-new",
            "refresh_token": "rotated-refresh",
            "expires_in": 900,
        }
        persisted = PersistedTokenSnapshot(
            token_id=1,
            account_id=1,
            app_id=1,
            access_token="brand-new",
            refresh_token="rotated-refresh",
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        with (
            patch(
                "apps.users.services.token_refresh.requests.post", return_value=response
            ) as mock_post,
            patch(
                "apps.users.services.token_refresh._persist_refresh_response",
                return_value=TokenRefreshResult(TokenRefreshStatus.APPLIED, persisted),
            ),
        ):
            new = refresh_oauth_token_sync(social_token, "https://token/")

        assert new == "brand-new"
        assert social_token.token == "brand-new"
        assert social_token.token_secret == "rotated-refresh"
        social_token.save.assert_not_called()
        mock_post.assert_called_once()

    def test_refresh_oauth_token_sync_raises_on_http_error(self):
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token_sync,
        )

        social_token = MagicMock(token="old-access", token_secret="refresh", app_id=1, account_id=1)
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("500")
        with patch("apps.users.services.token_refresh.requests.post", return_value=response):
            with pytest.raises(TokenRefreshError):
                refresh_oauth_token_sync(social_token, "https://token/")
