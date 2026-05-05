"""Base types for the API-key provider strategy registry."""

from __future__ import annotations

from typing import NamedTuple, TypedDict


class TenantDescriptor(NamedTuple):
    """A tenant the credential grants access to."""

    external_id: str
    canonical_name: str


class FormField(TypedDict):
    """A field in the Add/Edit dialog form schema."""

    key: str
    label: str
    type: str  # "text" | "password"
    required: bool
    editable_on_rotate: bool


class CredentialVerificationError(Exception):
    """Raised when the provider rejects a credential or the tenant is not accessible."""


class CredentialProviderStrategy:
    """Strategy for an API-key-authenticated provider.

    Subclasses set the class attributes and implement the four classmethods.
    All network IO lives in verify_and_discover and verify_for_tenant.
    """

    provider_id: str = ""
    display_name: str = ""
    form_fields: list[FormField] = []

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        """Serialize form fields into the opaque encrypted_credential string."""
        raise NotImplementedError

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        """Verify the credential and return all tenants it grants access to.

        Raises CredentialVerificationError on failure.
        """
        raise NotImplementedError

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        """Verify the credential still grants access to a known tenant.

        Used during PATCH (key rotation). Raises CredentialVerificationError
        on failure.
        """
        raise NotImplementedError
