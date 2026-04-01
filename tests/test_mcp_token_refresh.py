"""Test that MCP server refreshes expired OAuth tokens before materialization."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone


@pytest.mark.django_db
class TestMCPTokenRefresh:
    def test_expired_token_is_refreshed(self):
        from mcp_server.server import _resolve_oauth_credential
        mock_token = MagicMock()
        mock_token.token = "old-expired-token"
        mock_token.token_secret = "refresh-token"
        mock_token.expires_at = timezone.now() - timedelta(hours=1)
        mock_token.app = MagicMock()

        with patch("mcp_server.server.token_needs_refresh", return_value=True) as mock_needs, \
             patch("mcp_server.server.refresh_oauth_token", return_value="new-fresh-token") as mock_refresh:
            result = _resolve_oauth_credential(mock_token, "commcare")
            mock_needs.assert_called_once_with(mock_token.expires_at)
            mock_refresh.assert_called_once()
            assert result["value"] == "new-fresh-token"

    def test_valid_token_not_refreshed(self):
        from mcp_server.server import _resolve_oauth_credential
        mock_token = MagicMock()
        mock_token.token = "still-valid-token"
        mock_token.expires_at = timezone.now() + timedelta(hours=1)

        with patch("mcp_server.server.token_needs_refresh", return_value=False), \
             patch("mcp_server.server.refresh_oauth_token") as mock_refresh:
            result = _resolve_oauth_credential(mock_token, "commcare")
            mock_refresh.assert_not_called()
            assert result["value"] == "still-valid-token"

    def test_refresh_failure_returns_original_token(self):
        from apps.users.services.token_refresh import TokenRefreshError
        from mcp_server.server import _resolve_oauth_credential
        mock_token = MagicMock()
        mock_token.token = "maybe-still-works"
        mock_token.expires_at = timezone.now() - timedelta(minutes=1)

        with patch("mcp_server.server.token_needs_refresh", return_value=True), \
             patch("mcp_server.server.refresh_oauth_token", side_effect=TokenRefreshError("fail")):
            result = _resolve_oauth_credential(mock_token, "commcare")
            assert result["value"] == "maybe-still-works"
