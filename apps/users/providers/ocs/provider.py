"""OAuth2 provider for Open Chat Studio (OCS)."""

from __future__ import annotations

from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider

from apps.users.providers.ocs.views import OCSOAuth2Adapter


class OCSAccount(ProviderAccount):
    def get_avatar_url(self) -> str | None:
        return None

    def to_str(self) -> str:
        return self.account.extra_data.get("username", super().to_str())


class OCSProvider(OAuth2Provider):
    id = "ocs"
    name = "Open Chat Studio"
    account_class = OCSAccount
    oauth2_adapter_class = OCSOAuth2Adapter

    def get_default_scope(self) -> list[str]:
        return ["chatbots:read", "sessions:read", "files:read", "openid"]

    def extract_uid(self, data: dict) -> str:
        sub = data.get("sub")
        if not sub:
            raise ValueError(f"Cannot determine UID from OCS userinfo response: {data!r}")
        return str(sub)

    def extract_common_fields(self, data: dict) -> dict:
        return {
            "email": data.get("email") or None,
            "username": data.get("preferred_username") or data.get("email") or "",
            "first_name": data.get("given_name", ""),
            "last_name": data.get("family_name", ""),
        }


provider_classes = [OCSProvider]
