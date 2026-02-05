"""
Django app configuration for the CommCare OAuth provider.
"""

from django.apps import AppConfig


class CommCareProviderConfig(AppConfig):
    """App configuration for CommCare OAuth provider."""

    name = "apps.users.providers.commcare"
    verbose_name = "CommCare OAuth Provider"
