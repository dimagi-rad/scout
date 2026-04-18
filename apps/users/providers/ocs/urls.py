from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from apps.users.providers.ocs.provider import OCSProvider

urlpatterns = default_urlpatterns(OCSProvider)
