"""CommCare HQ API-key strategy."""

from __future__ import annotations

import httpx

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)

COMMCARE_API_BASE = "https://www.commcarehq.org"
COMMCARE_DOMAINS_URL = f"{COMMCARE_API_BASE}/api/user_domains/v1/"


def _auth_header(username: str, api_key: str) -> dict[str, str]:
    return {"Authorization": f"ApiKey {username}:{api_key}"}


async def _list_domains(username: str, api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(COMMCARE_DOMAINS_URL, headers=_auth_header(username, api_key))
    if resp.status_code in (401, 403):
        raise CredentialVerificationError(
            f"CommCare rejected the API key (HTTP {resp.status_code})"
        )
    if not resp.is_success:
        raise CredentialVerificationError(
            f"CommCare API returned unexpected status {resp.status_code}"
        )
    return resp.json().get("objects", [])


class CommCareStrategy(CredentialProviderStrategy):
    provider_id = "commcare"
    display_name = "CommCare HQ"
    form_fields: list[FormField] = [
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
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return f"{fields['username']}:{fields['api_key']}"

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        domain = fields["domain"]
        domains = await _list_domains(fields["username"], fields["api_key"])
        for entry in domains:
            if entry.get("domain_name") == domain:
                return [TenantDescriptor(domain, domain)]
        raise CredentialVerificationError(
            f"User '{fields['username']}' is not a member of domain '{domain}'"
        )

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        domains = await _list_domains(fields["username"], fields["api_key"])
        for entry in domains:
            if entry.get("domain_name") == external_id:
                return
        raise CredentialVerificationError(f"API key does not have access to domain '{external_id}'")
