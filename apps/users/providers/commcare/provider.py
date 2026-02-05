"""
CommCare OAuth2 provider for django-allauth.

This module defines the provider class that handles user data extraction
from CommCare's OAuth responses.

Example of implementing a custom OAuth2 provider for django-allauth.
Follow this pattern for other identity providers.
"""

from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider


class CommCareAccount(ProviderAccount):
    """
    Represents a user account from CommCare.

    Extracts and formats user information from the raw OAuth data
    for display in the Django admin and user interfaces.
    """

    def get_avatar_url(self) -> str | None:
        """Return the user's avatar URL if available."""
        # CommCare doesn't provide avatar URLs in the standard API
        return None

    def to_str(self) -> str:
        """Return a string representation of the account."""
        return self.account.extra_data.get("username", super().to_str())


class CommCareProvider(OAuth2Provider):
    """
    OAuth2 provider for CommCare HQ.

    Handles the extraction of user identity and profile information
    from CommCare's OAuth responses.

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

    def get_default_scope(self) -> list[str]:
        """Return the default OAuth scopes to request."""
        return ["read"]

    def extract_uid(self, data: dict) -> str:
        """
        Extract the unique user identifier from OAuth response.

        Args:
            data: Raw user data from the CommCare API.

        Returns:
            The unique user ID as a string.
        """
        return str(data["id"])

    def extract_common_fields(self, data: dict) -> dict:
        """
        Extract common user fields from OAuth response.

        Maps CommCare user data to Django user model fields.

        Args:
            data: Raw user data from the CommCare API.

        Returns:
            Dict with keys: email, username, first_name, last_name
        """
        return {
            "email": data.get("email"),
            "username": data.get("username"),
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
        }


# Required by django-allauth to discover this provider
provider_classes = [CommCareProvider]
