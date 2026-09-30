"""CommCare OAuth2 adapter and views for django-allauth."""

import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)

from apps.common.commcare_servers import COMMCARE_SERVERS, DEFAULT_SERVER

_WWW = COMMCARE_SERVERS[DEFAULT_SERVER]


class CommCareOAuth2Adapter(OAuth2Adapter):
    """OAuth2 adapter for CommCare HQ's www server (not configurable for self-hosted).

    Endpoints come from the server registry so sign-in, discovery and token
    refresh can never disagree about which HQ a credential belongs to.
    """

    provider_id = "commcare"

    # See: https://confluence.dimagi.com/display/commcarepublic/CommCare+HQ+APIs
    access_token_url = _WWW.token_url
    authorize_url = _WWW.authorize_url
    profile_url = _WWW.identity_url

    def complete_login(self, request, app, token, **kwargs):
        response = requests.get(
            self.profile_url,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=30,
        )
        response.raise_for_status()
        extra_data = response.json()

        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth2_login = OAuth2LoginView.adapter_view(CommCareOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(CommCareOAuth2Adapter)
