"""
URL configuration for CommCare OAuth provider.

These URLs are automatically included by django-allauth when the
provider is added to INSTALLED_APPS.

Standard allauth URL pattern:
- /accounts/commcare/login/ - Initiates OAuth flow
- /accounts/commcare/login/callback/ - OAuth callback endpoint
"""

from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from .provider import CommCareProvider

# Generate standard OAuth2 URL patterns for this provider
urlpatterns = default_urlpatterns(CommCareProvider)
