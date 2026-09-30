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
from mcp_server.loaders._urls import ProviderURLPolicy, UnsafeProviderURL


def _auth_header(username: str, api_key: str) -> dict[str, str]:
    return {"Authorization": f"ApiKey {username}:{api_key}"}


# A user's domain list is small; the bound only stops a looping ``next`` link.
_MAX_DOMAIN_PAGES = 50


async def _list_domains(domains_url: str, fields: dict[str, str]) -> list[dict]:
    """Every domain the key can see, following Tastypie's relative ``meta.next``.

    A domain past page 1 is a real membership, so stopping early would reject a
    valid key; shape drift and non-JSON bodies are rejections, never "no domains".
    """
    headers = _auth_header(fields["username"], fields["api_key"])
    policy = ProviderURLPolicy(domains_url)
    url: str | None = domains_url
    domains: list[dict] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for _page in range(_MAX_DOMAIN_PAGES):
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise CredentialVerificationError(
                    f"CommCare rejected the API key (HTTP {resp.status_code})"
                )
            if not resp.is_success:
                raise CredentialVerificationError(
                    f"CommCare API returned unexpected status {resp.status_code}"
                )
            try:
                payload = resp.json()
                domains.extend(payload["objects"])
                next_url = (payload.get("meta") or {}).get("next")
                url = policy.resolve(next_url, relative_to=url) if next_url else None
            except (ValueError, KeyError, TypeError, AttributeError, UnsafeProviderURL):
                raise CredentialVerificationError(
                    "CommCare returned an unexpected domain list"
                ) from None
            if url is None:
                return domains
    raise CredentialVerificationError("CommCare returned too many pages of domains")


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
    async def _domains(cls, fields: dict[str, str]) -> list[dict]:
        server = get_commcare_server(cls.server_for(fields))
        return await _list_domains(server.user_domains_url, fields)

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return f"{fields['username']}:{fields['api_key']}"

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        domain = fields["domain"]
        domains = await cls._domains(fields)
        for entry in domains:
            if entry.get("domain_name") == domain:
                return [TenantDescriptor(domain, domain)]
        raise CredentialVerificationError(
            f"User '{fields['username']}' is not a member of domain '{domain}'"
        )

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        domains = await cls._domains(fields)
        for entry in domains:
            if entry.get("domain_name") == external_id:
                return
        raise CredentialVerificationError(f"API key does not have access to domain '{external_id}'")
