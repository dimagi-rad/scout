"""
Tests for OAuth token storage, encryption, retrieval, and refresh.
"""

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from django.conf import settings


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
