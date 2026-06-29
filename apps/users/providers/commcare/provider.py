"""CommCare OAuth2 provider for django-allauth."""

from allauth.account.models import EmailAddress
from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider

from .views import CommCareOAuth2Adapter


class CommCareAccount(ProviderAccount):
    def get_avatar_url(self) -> str | None:
        # CommCare doesn't provide avatar URLs in the standard API
        return None

    def to_str(self) -> str:
        return self.account.extra_data.get("username", super().to_str())


class CommCareProvider(OAuth2Provider):
    """
    OAuth2 provider for CommCare HQ.

    To add this provider:
    1. Add 'apps.users.providers.commcare' to INSTALLED_APPS
    2. Create a SocialApp via Django admin with:
       - Provider: commcare
       - Client ID: Your CommCare OAuth client ID
       - Secret Key: Your CommCare OAuth client secret
    """

    id = "commcare"
    name = "CommCare"
    account_class = CommCareAccount
    oauth2_adapter_class = CommCareOAuth2Adapter

    def get_default_scope(self) -> list[str]:
        return ["access_apis"]

    def extract_uid(self, data: dict) -> str:
        return str(data["id"])

    def extract_common_fields(self, data: dict) -> dict:
        return {
            "email": data.get("email"),
            "username": data.get("username"),
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
        }

    def extract_email_addresses(self, data: dict) -> list[EmailAddress]:
        """Return the user's email as a verified address.

        allauth's base implementation returns ``[]``, which makes allauth fall
        back to creating an *unverified* EmailAddress from ``User.email``. CommCare
        HQ uses the email as the login identifier and confirms it at signup, so we
        trust it as verified. A verified address is what lets the cross-provider
        account-link/merge (see ``apps.users.signals.reconcile_existing_user_on_login``)
        recognise the same person across providers instead of stranding a
        duplicate, email-less account.
        """
        email = data.get("email")
        if not email:
            return []
        return [EmailAddress(email=email, verified=True, primary=True)]


provider_classes = [CommCareProvider]
