"""
Custom OAuth providers for the Scout platform.

This package contains custom OAuth provider implementations for systems
not supported by django-allauth out of the box.

To add a new custom provider:
1. Create a new subpackage (e.g., apps/users/providers/yourprovider/)
2. Implement provider.py with YourProvider and YourAccount classes
3. Implement views.py with YourOAuth2Adapter class
4. Implement urls.py with URL configuration
5. Add 'apps.users.providers.yourprovider' to INSTALLED_APPS

See the commcare package for a complete example.
"""
