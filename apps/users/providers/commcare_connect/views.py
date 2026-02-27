"""CommCare Connect OAuth2 adapter and views for django-allauth."""

import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)


class CommCareConnectOAuth2Adapter(OAuth2Adapter):
    """OAuth2 adapter for CommCare Connect (connect.dimagi.com)."""

    provider_id = "commcare_connect"

    access_token_url = "https://connect.dimagi.com/o/token/"
    authorize_url = "https://connect.dimagi.com/o/authorize/"
    profile_url = "https://connect.dimagi.com/api/users/me/"

    def complete_login(self, request, app, token, **kwargs):
        response = requests.get(
            self.profile_url,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=30,
        )
        response.raise_for_status()
        extra_data = response.json()
        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth2_login = OAuth2LoginView.adapter_view(CommCareConnectOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(CommCareConnectOAuth2Adapter)
