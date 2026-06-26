"""
Custom allauth social account adapter with Fernet token encryption.

Encrypts OAuth access tokens and refresh tokens before they are stored
in the database. Uses the same DB_CREDENTIAL_KEY Fernet key used for
project database credentials.
"""

from __future__ import annotations

import logging

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialToken
from allauth.socialaccount.providers import registry as providers_registry
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

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
        """Decrypt a token string. Returns empty string for empty or unreadable input."""
        if not ciphertext:
            return ""
        f = self._get_fernet()
        try:
            return f.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            logger.exception(
                "Failed to decrypt OAuth token — possible key rotation or data corruption"
            )
            return ""

    def serialize_instance(self, instance):
        """Encrypt token fields before serialization (storage)."""
        data = super().serialize_instance(instance)
        if isinstance(instance, SocialToken):
            if data.get("token"):
                data["token"] = self.encrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.encrypt_token(data["token_secret"])
        return data

    def deserialize_instance(self, model, data):
        """Decrypt token fields after deserialization (retrieval)."""
        if model is SocialToken:
            data = dict(data)  # don't mutate the original
            if data.get("token"):
                data["token"] = self.decrypt_token(data["token"])
            if data.get("token_secret"):
                data["token_secret"] = self.decrypt_token(data["token_secret"])
        return super().deserialize_instance(model, data)

    def pre_social_login(self, request, sociallogin):
        """Reject OAuth logins whose email is not in the per-provider allow-list.

        Runs after a successful OAuth callback but before any User/SocialAccount
        is created or login session established. Configured by the
        SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting (provider id -> list of
        allowed email domains). A provider with no entry (or an empty list) is
        unrestricted.

        For a provider WITH a non-empty allow-list, a login that returns no email
        is rejected (arch #258, finding 07#2): a missing email must not silently
        bypass a configured domain restriction. Providers Scout deliberately
        leaves open (Connect, OCS) carry no allow-list, so their no-email logins
        are unaffected by this gate.
        """
        provider = sociallogin.account.provider
        allowed = settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS.get(provider) or []
        if not allowed:
            return

        allowed_lower = [d.lower() for d in allowed]
        email = (sociallogin.user.email or "").strip().lower()
        # A configured allow-list means this provider is restricted; a no-email
        # login can't be confirmed to satisfy it, so reject rather than bypass.
        domain = email.rpartition("@")[2] if email else ""
        if domain and domain in allowed_lower:
            return

        provider_class = providers_registry.get_class(provider)
        provider_name = provider_class.name if provider_class else provider
        messages.error(
            request,
            "Sign-in with this account is not permitted. "
            f"Login using '{provider_name}' is restricted to {', '.join('@' + d for d in allowed_lower)} addresses.",
        )
        raise ImmediateHttpResponse(redirect("account_login"))


def encrypt_credential(plaintext: str) -> str:
    """Fernet-encrypt a credential string using DB_CREDENTIAL_KEY."""
    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str) -> str:
    """Fernet-decrypt a credential string using DB_CREDENTIAL_KEY."""
    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(ciphertext.encode()).decode()
