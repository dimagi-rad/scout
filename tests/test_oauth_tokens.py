"""
Tests for OAuth token storage, encryption, retrieval, and refresh.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from cryptography.fernet import Fernet
from django.conf import settings
from django.utils import timezone


class TestTokenStorageSettings:
    """Verify allauth token storage is enabled."""

    def test_socialaccount_store_tokens_enabled(self):
        """allauth should be configured to persist OAuth tokens."""
        assert settings.SOCIALACCOUNT_STORE_TOKENS is True


TEST_FERNET_KEY = Fernet.generate_key().decode()


class TestTokenEncryptionAdapter:
    """Test that the social account adapter encrypts/decrypts tokens."""

    @pytest.fixture
    def adapter(self):
        from apps.users.adapters import EncryptingSocialAccountAdapter

        return EncryptingSocialAccountAdapter()

    @patch.object(settings, "DB_CREDENTIAL_KEY", TEST_FERNET_KEY)
    def test_encrypt_decrypt_roundtrip(self, adapter):
        """Token should survive encrypt -> decrypt roundtrip."""
        original = "ya29.a0AfH6SMB_secret_token_value"
        encrypted = adapter.encrypt_token(original)
        assert encrypted != original
        assert adapter.decrypt_token(encrypted) == original

    @patch.object(settings, "DB_CREDENTIAL_KEY", TEST_FERNET_KEY)
    def test_encrypt_empty_string(self, adapter):
        """Empty string should return empty string without encryption."""
        assert adapter.encrypt_token("") == ""
        assert adapter.decrypt_token("") == ""

    @patch.object(settings, "DB_CREDENTIAL_KEY", TEST_FERNET_KEY)
    def test_encrypted_value_is_not_plaintext(self, adapter):
        """Encrypted output must not contain the original token."""
        original = "secret_token_12345"
        encrypted = adapter.encrypt_token(original)
        assert original not in encrypted

    @patch.object(settings, "DB_CREDENTIAL_KEY", "")
    def test_missing_key_raises(self, adapter):
        """Should raise ValueError when DB_CREDENTIAL_KEY is not set."""
        with pytest.raises(ValueError, match="DB_CREDENTIAL_KEY"):
            adapter.encrypt_token("some_token")


class TestCommCareConnectProvider:
    """Test the CommCare Connect OAuth provider is properly configured."""

    def test_provider_registered(self):
        """CommCare Connect provider should be discoverable by allauth."""
        from allauth.socialaccount import providers

        registry = providers.registry
        provider_cls = registry.get_class("commcare_connect")
        assert provider_cls is not None
        assert provider_cls.id == "commcare_connect"

    def test_provider_in_installed_apps(self):
        assert "apps.users.providers.commcare_connect" in settings.INSTALLED_APPS


class TestGetUserOAuthTokens:
    """Test the get_user_oauth_tokens helper in mcp_client."""

    def _make_social_token(self, provider, token, token_secret="refresh_tok", expires_at=None):
        """Build a mock SocialToken."""
        st = MagicMock()
        st.account.provider = provider
        st.token = token
        st.token_secret = token_secret
        st.expires_at = expires_at
        return st

    @patch("apps.agents.mcp_client.SocialToken")
    def test_returns_tokens_for_connected_providers(self, mock_social_token_cls):
        from apps.agents.mcp_client import get_user_oauth_tokens

        user = MagicMock()
        user.pk = 1

        mock_qs = MagicMock()
        mock_social_token_cls.objects.filter.return_value = mock_qs
        mock_qs.select_related.return_value = [
            self._make_social_token("commcare", "hq_token_123"),
            self._make_social_token("commcare_connect", "connect_token_456"),
        ]

        result = async_to_sync(get_user_oauth_tokens)(user)
        assert result == {
            "commcare": "hq_token_123",
            "commcare_connect": "connect_token_456",
        }

    @patch("apps.agents.mcp_client.SocialToken")
    def test_returns_empty_dict_for_no_tokens(self, mock_social_token_cls):
        from apps.agents.mcp_client import get_user_oauth_tokens

        user = MagicMock()
        user.pk = 1

        mock_qs = MagicMock()
        mock_social_token_cls.objects.filter.return_value = mock_qs
        mock_qs.select_related.return_value = []

        result = async_to_sync(get_user_oauth_tokens)(user)
        assert result == {}

    @patch("apps.agents.mcp_client.SocialToken")
    def test_skips_non_commcare_providers(self, mock_social_token_cls):
        from apps.agents.mcp_client import get_user_oauth_tokens

        user = MagicMock()
        user.pk = 1

        mock_qs = MagicMock()
        mock_social_token_cls.objects.filter.return_value = mock_qs
        mock_qs.select_related.return_value = [
            self._make_social_token("google", "google_token"),
            self._make_social_token("commcare", "hq_token"),
        ]

        result = async_to_sync(get_user_oauth_tokens)(user)
        assert result == {"commcare": "hq_token"}

    def test_returns_empty_dict_for_none_user(self):
        from apps.agents.mcp_client import get_user_oauth_tokens

        result = async_to_sync(get_user_oauth_tokens)(None)
        assert result == {}


class TestTokenRefresh:
    """Test the OAuth token refresh service."""

    @patch("apps.users.services.token_refresh.requests.post")
    def test_refresh_updates_token(self, mock_post):
        from apps.users.services.token_refresh import refresh_oauth_token

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value={
                    "access_token": "new_access_token",
                    "refresh_token": "new_refresh_token",
                    "expires_in": 3600,
                }
            ),
        )
        mock_post.return_value.raise_for_status = MagicMock()

        social_token = MagicMock()
        social_token.token = "old_access_token"
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        # CommCare HQ token URL
        token_url = "https://www.commcarehq.org/oauth/token/"

        result = refresh_oauth_token(social_token, token_url)

        assert result == "new_access_token"
        assert social_token.token == "new_access_token"
        assert social_token.token_secret == "new_refresh_token"
        social_token.save.assert_called_once()

    @patch("apps.users.services.token_refresh.requests.post")
    def test_refresh_failure_raises(self, mock_post):
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token,
        )

        mock_post.return_value = MagicMock(status_code=400)
        mock_post.return_value.raise_for_status.side_effect = Exception("Bad Request")

        social_token = MagicMock()
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        with pytest.raises(TokenRefreshError):
            refresh_oauth_token(social_token, "https://example.com/oauth/token/")

    def test_token_needs_refresh_when_expiring_soon(self):
        from apps.users.services.token_refresh import token_needs_refresh

        soon = timezone.now() + timedelta(minutes=3)
        assert token_needs_refresh(soon) is True

    def test_token_does_not_need_refresh_when_fresh(self):
        from apps.users.services.token_refresh import token_needs_refresh

        later = timezone.now() + timedelta(hours=1)
        assert token_needs_refresh(later) is False

    def test_token_needs_refresh_when_expired(self):
        from apps.users.services.token_refresh import token_needs_refresh

        past = timezone.now() - timedelta(hours=1)
        assert token_needs_refresh(past) is True

    def test_token_needs_refresh_when_none(self):
        from apps.users.services.token_refresh import token_needs_refresh

        assert token_needs_refresh(None) is False


@pytest.mark.django_db
class TestGraphOAuthConfig:
    """Test that build_agent_graph accepts oauth_tokens gracefully."""

    @patch("apps.agents.graph.base.ChatAnthropic")
    @patch("apps.agents.graph.base.KnowledgeRetriever")
    def test_build_graph_accepts_oauth_tokens(self, mock_kr, mock_llm, tenant_membership):
        """build_agent_graph should accept oauth_tokens without error."""
        from apps.agents.graph.base import build_agent_graph

        mock_kr_instance = MagicMock()
        mock_kr_instance.retrieve.return_value = ""
        mock_kr.return_value = mock_kr_instance

        mock_llm_instance = MagicMock()
        mock_llm_instance.bind_tools.return_value = mock_llm_instance
        mock_llm.return_value = mock_llm_instance

        # Should not raise
        graph = build_agent_graph(
            tenant_membership=tenant_membership,
            oauth_tokens={"commcare": "test_token"},
        )
        assert graph is not None
