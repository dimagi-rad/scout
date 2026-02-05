"""
CommCare OAuth2 provider for django-allauth.

This is an example custom OAuth provider implementation that can be used
as a template for adding other custom providers to the Scout platform.

CommCare HQ (https://www.commcarehq.org/) is a mobile data collection
platform used widely in global health and development.

Usage:
    1. Add 'apps.users.providers.commcare' to INSTALLED_APPS
    2. Configure OAuth app credentials via Django admin (SocialApp model)
    3. The provider will be available at /accounts/commcare/login/

For CommCare OAuth documentation, see:
https://confluence.dimagi.com/display/commcarepublic/CommCare+HQ+APIs
"""

default_app_config = "apps.users.providers.commcare.apps.CommCareProviderConfig"
