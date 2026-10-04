"""CommCare HQ API-key strategy."""

from __future__ import annotations

import httpx

from apps.common.commcare_servers import (
    COMMCARE_SERVERS,
    UnknownCommCareServer,
    get_commcare_server,
)
from apps.users.models import TenantConnection
from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)
from apps.users.services.tenant_listing import commcare as commcare_listing
from apps.users.services.tenant_listing.paginator import list_tenants
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    RequestTimedOut,
    TenantListError,
    UnsafeNextURL,
    UpstreamStatus,
    UpstreamUnreachable,
)

# A user's domain list is small; the bounds stop a looping or trickling upstream
# from holding a credential add open.
_MAX_DOMAIN_PAGES = 50
_LISTING_BUDGET_SECONDS = 30.0
_REQUEST_TIMEOUT_SECONDS = 15.0
_UNEXPECTED = "CommCare returned an unexpected domain list"


async def _list_domains(server_key: str, credential: str) -> list[TenantDescriptor]:
    """Every domain the key can see, following Tastypie's relative ``meta.next``.

    A domain past page 1 is a real membership, so stopping early would reject a
    valid key; shape drift and non-JSON bodies are rejections, never "no domains".
    """
    request = commcare_listing.list_request(server_key, TenantConnection.API_KEY, credential)
    if request is None:
        raise CredentialVerificationError("Enter a CommCare username and API key")
    try:
        async with httpx.AsyncClient() as client:
            return await list_tenants(
                client,
                request,
                commcare_listing.decode_page,
                budget_seconds=_LISTING_BUDGET_SECONDS,
                max_pages=_MAX_DOMAIN_PAGES,
                request_timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except UpstreamStatus as error:
        if error.status_code in (401, 403):
            raise CredentialVerificationError(
                f"CommCare rejected the API key (HTTP {error.status_code})"
            ) from None
        raise CredentialVerificationError(
            f"CommCare API returned unexpected status {error.status_code}"
        ) from None
    except (UpstreamUnreachable, RequestTimedOut):
        raise CredentialVerificationError("CommCare could not be reached") from None
    except (MalformedTenantList, UnsafeNextURL):
        raise CredentialVerificationError(_UNEXPECTED) from None
    except TenantListError:
        raise CredentialVerificationError("CommCare's domain list did not finish") from None


class CommCareStrategy(CredentialProviderStrategy):
    provider_id = "commcare"
    display_name = "CommCare HQ"
    form_fields: list[FormField] = [
        {
            "key": "server",
            "label": "CommCare HQ server",
            "type": "select",
            "required": False,
            "editable_on_rotate": False,
            "options": [
                {"value": server.key, "label": f"{server.label} ({server.host})"}
                for server in COMMCARE_SERVERS.values()
            ],
        },
        {
            "key": "domain",
            "label": "Domain",
            "type": "text",
            "required": True,
            "editable_on_rotate": False,
        },
        {
            "key": "username",
            "label": "Username",
            "type": "text",
            "required": True,
            "editable_on_rotate": True,
        },
        {
            "key": "api_key",
            "label": "API Key",
            "type": "password",
            "required": True,
            "editable_on_rotate": True,
        },
    ]

    @classmethod
    def server_for(cls, fields: dict[str, str]) -> str:
        server = (fields.get("server") or "").strip()
        try:
            return get_commcare_server(server).key
        except UnknownCommCareServer:
            raise CredentialVerificationError(f"Unknown CommCare HQ server '{server}'") from None

    @classmethod
    def server_label(cls, server: str) -> str:
        return get_commcare_server(server).label if server else ""

    @classmethod
    async def _domains(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        return await _list_domains(cls.server_for(fields), cls.pack_credential(fields))

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return f"{fields['username']}:{fields['api_key']}"

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        domain = fields["domain"]
        domains = await cls._domains(fields)
        for entry in domains:
            if entry.external_id == domain:
                return [TenantDescriptor(domain, domain)]
        raise CredentialVerificationError(
            f"User '{fields['username']}' is not a member of domain '{domain}'"
        )

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        domains = await cls._domains(fields)
        for entry in domains:
            if entry.external_id == external_id:
                return
        raise CredentialVerificationError(f"API key does not have access to domain '{external_id}'")
