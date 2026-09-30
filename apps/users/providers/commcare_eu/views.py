"""CommCare HQ (EU) OAuth2 adapter and views for django-allauth."""

from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView

from apps.common.commcare_servers import COMMCARE_SERVERS
from apps.users.providers.commcare.views import CommCareOAuth2Adapter

_EU = COMMCARE_SERVERS["eu"]


class CommCareEUOAuth2Adapter(CommCareOAuth2Adapter):
    provider_id = _EU.provider_id
    access_token_url = _EU.token_url
    authorize_url = _EU.authorize_url
    profile_url = _EU.identity_url


oauth2_login = OAuth2LoginView.adapter_view(CommCareEUOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(CommCareEUOAuth2Adapter)
