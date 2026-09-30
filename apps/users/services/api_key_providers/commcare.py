"""CommCare HQ API-key strategy."""

from __future__ import annotations

import httpx

from apps.common.commcare_servers import (
    COMMCARE_SERVERS,
    UnknownCommCareServer,
    get_commcare_server,
)
from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)


def _auth_header(username: str, api_key: str) -> dict[str, str]:
    return {"Authorization": f"ApiKey {username}:{api_key}"}


def _domains_url(fields: dict[str, str]) -> str:
    return get_commcare_server(CommCareStrategy.server_for(fields)).user_domains_url


async def _list_domains(fields: dict[str, str]) -> list[dict]:
    headers = _auth_header(fields["username"], fields["api_key"])
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(_domains_url(fields), headers=headers)
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
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return f"{fields['username']}:{fields['api_key']}"

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        domain = fields["domain"]
        domains = await _list_domains(fields)
        for entry in domains:
            if entry.get("domain_name") == domain:
                return [TenantDescriptor(domain, domain)]
        raise CredentialVerificationError(
            f"User '{fields['username']}' is not a member of domain '{domain}'"
        )

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        domains = await _list_domains(fields)
        for entry in domains:
            if entry.get("domain_name") == external_id:
                return
        raise CredentialVerificationError(f"API key does not have access to domain '{external_id}'")
