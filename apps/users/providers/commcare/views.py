"""CommCare OAuth2 adapter and views for django-allauth."""

import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)


class CommCareOAuth2Adapter(OAuth2Adapter):
    """OAuth2 adapter for CommCare HQ (production instance; not configurable for self-hosted)."""

    provider_id = "commcare"

    # See: https://confluence.dimagi.com/display/commcarepublic/CommCare+HQ+APIs
    access_token_url = "https://www.commcarehq.org/oauth/token/"  # noqa: S105 — OAuth endpoint URL, not a credential
    authorize_url = "https://www.commcarehq.org/oauth/authorize/"
    profile_url = "https://www.commcarehq.org/api/v0.5/identity/"

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
