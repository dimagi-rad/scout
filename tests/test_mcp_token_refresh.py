"""Test that credential resolution refreshes expired OAuth tokens."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.utils import timezone


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
            mock_needs.assert_called_once_with(mock_token.expires_at)
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
    async def test_refresh_failure_fails_closed(self):
        """arch #252 (14#4): a refresh failure at/near expiry must fail closed
        with actionable reconnect guidance, not fall back to a stale token."""
        from apps.users.services.credential_resolver import (
            CredentialResolutionError,
            _aresolve_oauth_credential,
        )
        from apps.users.services.token_refresh import TokenRefreshError
        from mcp_server.envelope import AUTH_TOKEN_EXPIRED

        mock_token = MagicMock()
        mock_token.token = "known-stale-token"
        mock_token.expires_at = timezone.now() - timedelta(minutes=1)

        with (
            patch("apps.users.services.credential_resolver.token_needs_refresh", return_value=True),
            patch(
                "apps.users.services.credential_resolver.refresh_oauth_token",
                new_callable=AsyncMock,
                side_effect=TokenRefreshError("fail"),
            ),
            pytest.raises(CredentialResolutionError) as exc,
        ):
            await _aresolve_oauth_credential(mock_token, "commcare")
        assert exc.value.code == AUTH_TOKEN_EXPIRED
        assert "reconnect" in exc.value.message.lower()

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
