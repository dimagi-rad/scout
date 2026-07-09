"""
Tests for OAuth token storage, encryption, retrieval, and refresh.
"""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.sites.models import Site
from django.utils import timezone

from apps.users.services.credential_resolver import _social_token_qs


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


class TestTokenRefresh:
    """Test the OAuth token refresh service."""

    @pytest.mark.asyncio
    async def test_refresh_updates_token(self, httpx_mock):
        from apps.users.services.token_refresh import refresh_oauth_token

        token_url = "https://www.commcarehq.org/oauth/token/"
        httpx_mock.add_response(
            url=token_url,
            method="POST",
            json={
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 3600,
            },
        )

        social_token = MagicMock()
        social_token.token = "old_access_token"
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"
        social_token.asave = AsyncMock()

        result = await refresh_oauth_token(social_token, token_url)

        assert result == "new_access_token"
        assert social_token.token == "new_access_token"
        assert social_token.token_secret == "new_refresh_token"
        social_token.asave.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_failure_raises(self, httpx_mock):
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token,
        )

        token_url = "https://example.com/oauth/token/"
        httpx_mock.add_response(url=token_url, method="POST", status_code=400)

        social_token = MagicMock()
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        with pytest.raises(TokenRefreshError):
            await refresh_oauth_token(social_token, token_url)

    @pytest.mark.asyncio
    async def test_refresh_400_logs_warning_not_error(self, httpx_mock, caplog):
        """A 400 invalid_grant (dead token) is expected: WARNING, no exception log."""
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token,
        )

        token_url = "https://example.com/oauth/token/"
        httpx_mock.add_response(
            url=token_url,
            method="POST",
            status_code=400,
            json={"error": "invalid_grant"},
        )

        social_token = MagicMock()
        social_token.token_secret = "dead_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        with caplog.at_level(logging.DEBUG, logger="apps.users.services.token_refresh"):
            with pytest.raises(TokenRefreshError):
                await refresh_oauth_token(social_token, token_url)

        records = [r for r in caplog.records if r.name == "apps.users.services.token_refresh"]
        assert records, "expected a log record"
        assert all(r.levelno == logging.WARNING for r in records)
        assert not any(r.levelno >= logging.ERROR for r in records)
        assert not any(r.exc_info for r in records)
        assert "invalid_grant" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_500_logs_exception(self, httpx_mock, caplog):
        """A 5xx is genuinely unexpected: keep exception-level logging."""
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token,
        )

        token_url = "https://example.com/oauth/token/"
        httpx_mock.add_response(url=token_url, method="POST", status_code=503)

        social_token = MagicMock()
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        with caplog.at_level(logging.DEBUG, logger="apps.users.services.token_refresh"):
            with pytest.raises(TokenRefreshError):
                await refresh_oauth_token(social_token, token_url)

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR/exception-level log record"
        assert any(r.exc_info for r in error_records)

    @pytest.mark.asyncio
    async def test_refresh_network_error_logs_exception(self, httpx_mock, caplog):
        """A network error is unexpected: keep exception-level logging."""
        from apps.users.services.token_refresh import (
            TokenRefreshError,
            refresh_oauth_token,
        )

        token_url = "https://example.com/oauth/token/"
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=token_url)

        social_token = MagicMock()
        social_token.token_secret = "old_refresh_token"
        social_token.app.client_id = "client_123"
        social_token.app.secret = "secret_456"

        with caplog.at_level(logging.DEBUG, logger="apps.users.services.token_refresh"):
            with pytest.raises(TokenRefreshError):
                await refresh_oauth_token(social_token, token_url)

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR/exception-level log record"
        assert any(r.exc_info for r in error_records)

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
def test_social_token_qs_ocs_matches_only_ocs_tokens(user):
    site = Site.objects.get_current()
    ocs_app = SocialApp.objects.create(provider="ocs", name="OCS", client_id="c", secret="s")
    ocs_app.sites.add(site)
    ocs_account = SocialAccount.objects.create(user=user, provider="ocs", uid="u-1")
    ocs_token = SocialToken.objects.create(app=ocs_app, account=ocs_account, token="ocs-tok")

    cc_app = SocialApp.objects.create(provider="commcare", name="CC", client_id="c", secret="s")
    cc_app.sites.add(site)
    cc_account = SocialAccount.objects.create(user=user, provider="commcare", uid="u-2")
    SocialToken.objects.create(app=cc_app, account=cc_account, token="cc-tok")

    result = list(_social_token_qs(user, "ocs"))
    assert len(result) == 1
    assert result[0].id == ocs_token.id
