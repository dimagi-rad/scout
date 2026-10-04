"""Open Chat Studio API-key strategy."""

from __future__ import annotations

import httpx
from django.conf import settings

from apps.users.models import TenantConnection
from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)
from apps.users.services.tenant_listing import ocs as ocs_listing
from apps.users.services.tenant_listing.paginator import list_tenants
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    RequestTimedOut,
    TenantListError,
    UnsafeListingOrigin,
    UnsafeNextURL,
    UpstreamStatus,
    UpstreamUnreachable,
)

OCS_DEFAULT_URL = "https://www.openchatstudio.com"

_MAX_EXPERIMENT_PAGES = 100
# Bounds a looping or trickling upstream, as for CommCare keys.
_LISTING_BUDGET_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 30.0
_UNSAFE_NEXT = "OCS returned an untrusted pagination link"
_UNEXPECTED = "OCS returned an unexpected experiment list"


async def _list_experiments(api_key: str) -> list[TenantDescriptor]:
    """Every experiment the key can see, across all pages.

    Raises CredentialVerificationError on auth failure, an unexpected status or
    list, an unreachable server, or a list that does not finish.
    """
    base_url = getattr(settings, "OCS_URL", OCS_DEFAULT_URL)
    request = ocs_listing.list_request(base_url, TenantConnection.API_KEY, api_key)
    try:
        async with httpx.AsyncClient() as client:
            return await list_tenants(
                client,
                request,
                ocs_listing.decode_page,
                budget_seconds=_LISTING_BUDGET_SECONDS,
                max_pages=_MAX_EXPERIMENT_PAGES,
                request_timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except UpstreamStatus as error:
        if error.status_code in (401, 403):
            raise CredentialVerificationError(
                f"OCS rejected the API key (HTTP {error.status_code})"
            ) from None
        raise CredentialVerificationError(
            f"OCS API returned unexpected status {error.status_code}"
        ) from None
    except UnsafeListingOrigin as error:
        raise CredentialVerificationError(
            f"OCS_URL is not a safe provider origin: {error}"
        ) from None
    except UnsafeNextURL:
        raise CredentialVerificationError(_UNSAFE_NEXT) from None
    except (UpstreamUnreachable, RequestTimedOut):
        raise CredentialVerificationError("OCS could not be reached") from None
    except MalformedTenantList:
        raise CredentialVerificationError(_UNEXPECTED) from None
    except TenantListError:
        raise CredentialVerificationError("OCS experiment list did not finish") from None


class OCSStrategy(CredentialProviderStrategy):
    provider_id = "ocs"
    display_name = "Open Chat Studio"
    form_fields: list[FormField] = [
        {
            "key": "api_key",
            "label": "API Key",
            "type": "password",
            "required": True,
            "editable_on_rotate": True,
        },
        {
            "key": "team_name",
            "label": "Team name (auto-detected if left blank)",
            "type": "text",
            "required": False,
            "editable_on_rotate": False,
        },
    ]

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return fields["api_key"]

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        experiments = await _list_experiments(fields["api_key"])
        if not experiments:
            raise CredentialVerificationError(
                "OCS API key is valid but has no experiments accessible"
            )
        return experiments

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        experiments = await _list_experiments(fields["api_key"])
        for experiment in experiments:
            if experiment.external_id == external_id:
                return
        raise CredentialVerificationError(
            f"API key does not have access to experiment '{external_id}'"
        )
