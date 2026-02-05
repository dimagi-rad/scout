"""
CommCare OAuth2 adapter and views for django-allauth.

This module defines the OAuth2 adapter that handles the OAuth flow:
- Authorization URL (where to redirect users for login)
- Token URL (where to exchange auth code for tokens)
- Profile URL (where to fetch user information)

The adapter translates between CommCare's OAuth implementation and
django-allauth's expected interface.
"""

import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)

from .provider import CommCareProvider


class CommCareOAuth2Adapter(OAuth2Adapter):
    """
    OAuth2 adapter for CommCare HQ.

    Configures the OAuth endpoints and handles the token exchange
    and user profile fetching.

    The URLs below are for the production CommCare HQ instance.
    For self-hosted CommCare, these would need to be configurable
    via Django settings.
    """

    provider_id = CommCareProvider.id

    # CommCare OAuth endpoints
    # See: https://confluence.dimagi.com/display/commcarepublic/CommCare+HQ+APIs
    access_token_url = "https://www.commcarehq.org/oauth/token/"
    authorize_url = "https://www.commcarehq.org/oauth/authorize/"
    profile_url = "https://www.commcarehq.org/api/v0.5/identity/"

    def complete_login(self, request, app, token, **kwargs):
        """
        Complete the OAuth login by fetching user profile.

        Called after successfully obtaining an access token.
        Fetches the user's profile from CommCare and creates
        a SocialLogin object.

        Args:
            request: The Django request object.
            app: The SocialApp model instance with client credentials.
            token: The OAuth access token.
            **kwargs: Additional arguments (includes 'response' with token data).

        Returns:
            A SocialLogin object with the user's profile data.
        """
        # Fetch user profile from CommCare API
        response = requests.get(
            self.profile_url,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=30,
        )
        response.raise_for_status()
        extra_data = response.json()

        return self.get_provider().sociallogin_from_response(request, extra_data)


# View instances for URL routing
oauth2_login = OAuth2LoginView.adapter_view(CommCareOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(CommCareOAuth2Adapter)
