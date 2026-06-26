"""CommCare Connect OAuth2 adapter and views for django-allauth."""

import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)
from django.conf import settings


class CommCareConnectOAuth2Adapter(OAuth2Adapter):
    """OAuth2 adapter for CommCare Connect."""

    provider_id = "commcare_connect"

    @property
    def authorize_url(self) -> str:
        return f"{settings.CONNECT_OAUTH_URL.rstrip('/')}/o/authorize/"

    @property
    def access_token_url(self) -> str:
        return f"{settings.CONNECT_OAUTH_URL.rstrip('/')}/o/token/"

    @property
    def profile_url(self) -> str:
        return f"{settings.CONNECT_API_URL.rstrip('/')}/api/users/me/"

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
