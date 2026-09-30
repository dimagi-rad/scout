"""CommCare HQ API-key strategy."""

from __future__ import annotations

import asyncio
import time

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


# A user's domain list is small; the bounds stop a looping or trickling upstream
# from holding a credential add open.
_MAX_DOMAIN_PAGES = 50
_LISTING_BUDGET_SECONDS = 30.0
_REQUEST_TIMEOUT_SECONDS = 15.0
_UNEXPECTED = "CommCare returned an unexpected domain list"


def _page(payload) -> tuple[list, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("objects"), list):
        raise CredentialVerificationError(_UNEXPECTED)
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise CredentialVerificationError(_UNEXPECTED)
    next_url = meta.get("next")
    if next_url is not None and not isinstance(next_url, str):
        raise CredentialVerificationError(_UNEXPECTED)
    return payload["objects"], next_url


async def _list_domains(domains_url: str, fields: dict[str, str]) -> list[dict]:
    """Every domain the key can see, following Tastypie's relative ``meta.next``.

    A domain past page 1 is a real membership, so stopping early would reject a
    valid key; shape drift and non-JSON bodies are rejections, never "no domains".
    """
    headers = _auth_header(fields["username"], fields["api_key"])
    policy = ProviderURLPolicy(domains_url)
    deadline = time.monotonic() + _LISTING_BUDGET_SECONDS
    url = domains_url
    seen: set[str] = set()
    domains: list[dict] = []
    async with httpx.AsyncClient() as client:
        for _page_number in range(_MAX_DOMAIN_PAGES):
            remaining = deadline - time.monotonic()
            if url in seen or remaining <= 0:
                break
            seen.add(url)
            try:
                # The key must never follow a redirect past the origin policy.
                resp = await asyncio.wait_for(
                    client.get(
                        url,
                        headers=headers,
                        follow_redirects=False,
                        timeout=min(_REQUEST_TIMEOUT_SECONDS, remaining),
                    ),
                    timeout=remaining,
                )
            except (httpx.RequestError, TimeoutError):
                raise CredentialVerificationError("CommCare could not be reached") from None
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
            except ValueError:
                raise CredentialVerificationError(_UNEXPECTED) from None
            objects, next_url = _page(payload)
            domains.extend(objects)
            if not next_url:
                return domains
            try:
                url = policy.resolve(next_url, relative_to=url)
            except UnsafeProviderURL:
                raise CredentialVerificationError(_UNEXPECTED) from None
    raise CredentialVerificationError("CommCare's domain list did not finish")


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
