"""CommCare Connect OAuth2 provider for django-allauth."""

from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider

from .views import CommCareConnectOAuth2Adapter


class CommCareConnectAccount(ProviderAccount):
    def get_avatar_url(self) -> str | None:
        return None

    def to_str(self) -> str:
        return self.account.extra_data.get("username", super().to_str())


class CommCareConnectProvider(OAuth2Provider):
    """
    OAuth2 provider for CommCare Connect.

    To add this provider:
    1. Add 'apps.users.providers.commcare_connect' to INSTALLED_APPS
    2. Create a SocialApp via Django admin with:
       - Provider: commcare_connect
       - Client ID: Your CommCare Connect OAuth client ID
       - Secret Key: Your CommCare Connect OAuth client secret
    """

    id = "commcare_connect"
    name = "CommCare Connect"
    account_class = CommCareConnectAccount
    oauth2_adapter_class = CommCareConnectOAuth2Adapter

    def get_default_scope(self) -> list[str]:
        return ["read", "export"]

    def extract_uid(self, data: dict) -> str:
        # /api/users/me/ returns {"name": "...", "url": "http://.../api/users/123/"}
        # Extract the pk from the URL as the uid
        if "id" in data:
            return str(data["id"])
        url = data.get("url", "")
        pk = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        return pk

    def extract_common_fields(self, data: dict) -> dict:
        name = data.get("name", "")
        return {
            "email": data.get("email", ""),
            "username": data.get("username", name),
            "first_name": name,
            "last_name": "",
        }


provider_classes = [CommCareConnectProvider]
