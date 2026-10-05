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

from apps.users.services import ocs_team_flow


class OCSTeamMismatch(OAuth2Error):
    """OCS issued a token for another team than the one the flow was pinned to."""

    def __init__(self, requested: str, claims: dict):
        self.requested = requested
        self.got = str(claims.get("team") or "").strip()
        self.claims = claims
        super().__init__(f"OCS returned team {self.got!r} for a flow pinned to {requested!r}")


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
        requested = getattr(request, "ocs_requested_team", "")
        if requested and str(extra_data.get("team") or "").strip() != requested:
            # Raised before allauth's lookup, which would otherwise store the token
            # on an existing account for the returned team, reviving one the user removed.
            raise OCSTeamMismatch(requested, extra_data)
        return self.get_provider().sociallogin_from_response(request, extra_data)


class OCSOAuth2CallbackView(OAuth2CallbackView):
    def _get_state(self, request, provider):
        # complete_login is not handed the OAuth state, but it must see the pinned team.
        state, response = super()._get_state(request, provider)
        request.ocs_requested_team = (state or {}).get(ocs_team_flow.REQUESTED_TEAM_STATE_KEY, "")
        return state, response


oauth2_login = OAuth2LoginView.adapter_view(OCSOAuth2Adapter)
oauth2_callback = OCSOAuth2CallbackView.adapter_view(OCSOAuth2Adapter)
