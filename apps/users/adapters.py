"""
Custom allauth social account adapter with Fernet token encryption.

Encrypts OAuth access tokens and refresh tokens before they are stored
in the database. Uses the same DB_CREDENTIAL_KEY Fernet key used for
project database credentials.
"""

from __future__ import annotations

import logging

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from cryptography.fernet import Fernet
from django.conf import settings

logger = logging.getLogger(__name__)


class EncryptingSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Adapter that Fernet-encrypts SocialToken fields at rest."""

    def _get_fernet(self) -> Fernet:
        key = settings.DB_CREDENTIAL_KEY
        if not key:
            raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
        return Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt_token(self, plaintext: str) -> str:
        """Encrypt a token string. Returns empty string for empty input."""
        if not plaintext:
            return ""
        f = self._get_fernet()
        return f.encrypt(plaintext.encode()).decode()

    def decrypt_token(self, ciphertext: str) -> str:
        """Decrypt a token string. Returns empty string for empty input."""
        if not ciphertext:
            return ""
        f = self._get_fernet()
        return f.decrypt(ciphertext.encode()).decode()

    def serialize_instance(self, instance):
        """Encrypt token fields before serialization (storage)."""
        from allauth.socialaccount.models import SocialToken

        data = super().serialize_instance(instance)
        if isinstance(instance, SocialToken):
            if data.get("token"):
                data["token"] = self.encrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.encrypt_token(data["token_secret"])
        return data

    def deserialize_instance(self, model, data):
        """Decrypt token fields after deserialization (retrieval)."""
        from allauth.socialaccount.models import SocialToken

        if model is SocialToken:
            data = dict(data)  # don't mutate the original
            if data.get("token"):
                data["token"] = self.decrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.decrypt_token(data["token_secret"])
        return super().deserialize_instance(model, data)
