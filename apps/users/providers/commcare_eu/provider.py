"""CommCare HQ (EU) OAuth2 provider for django-allauth."""

from apps.users.providers.commcare.provider import CommCareProvider

from .views import CommCareEUOAuth2Adapter


class CommCareEUProvider(CommCareProvider):
    id = CommCareEUOAuth2Adapter.provider_id
    name = "CommCare HQ (EU)"
    oauth2_adapter_class = CommCareEUOAuth2Adapter


provider_classes = [CommCareEUProvider]
