"""OAuth2 adapter views for Open Chat Studio."""

from __future__ import annotations

import requests
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)
from django.conf import settings


class OCSOAuth2Adapter(OAuth2Adapter):
    provider_id = "ocs"

    @property
    def authorize_url(self) -> str:
        return f"{settings.OCS_URL.rstrip('/')}/o/authorize/"

    @property
    def access_token_url(self) -> str:
        return f"{settings.OCS_URL.rstrip('/')}/o/token/"

    @property
    def profile_url(self) -> str:
        return f"{settings.OCS_URL.rstrip('/')}/o/userinfo/"

    def complete_login(self, request, app, token, **kwargs):
        response = requests.get(
            self.profile_url,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=30,
        )
        if response.status_code >= 400:
            raise OAuth2Error(f"OCS userinfo request failed: HTTP {response.status_code}")
        extra_data = response.json()
        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth2_login = OAuth2LoginView.adapter_view(OCSOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(OCSOAuth2Adapter)
