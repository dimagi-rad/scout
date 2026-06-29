"""URL configuration for CommCare OAuth provider."""

from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from .provider import CommCareProvider

urlpatterns = default_urlpatterns(CommCareProvider)
