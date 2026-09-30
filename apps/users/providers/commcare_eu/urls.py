from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from .provider import CommCareEUProvider

urlpatterns = default_urlpatterns(CommCareEUProvider)
