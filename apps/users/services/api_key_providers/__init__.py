"""Provider-strategy abstraction for API-key authentication.

Each concrete strategy describes how to verify a personal API key for a
provider (CommCare, OCS, Connect) and discover the tenant(s) that key
grants access to. The strategy registry in registry.py maps provider IDs
to strategy classes; views and the frontend dialog dispatch through it.
"""

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)

__all__ = [
    "CredentialProviderStrategy",
    "CredentialVerificationError",
    "FormField",
    "TenantDescriptor",
]
