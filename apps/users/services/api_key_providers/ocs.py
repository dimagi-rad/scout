"""Open Chat Studio API-key strategy."""

from __future__ import annotations

import httpx
from django.conf import settings

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)

OCS_DEFAULT_URL = "https://www.openchatstudio.com"


def _auth_header(api_key: str) -> dict[str, str]:
    return {"X-api-key": api_key}


def _experiments_url() -> str:
    base = getattr(settings, "OCS_URL", OCS_DEFAULT_URL).rstrip("/")
    return f"{base}/api/experiments/"


async def _list_experiments(api_key: str) -> list[dict]:
    """Paginate through /api/experiments/ and return all results.

    Raises CredentialVerificationError on auth failure or unexpected status.
    """
    headers = _auth_header(api_key)
    results: list[dict] = []
    url: str | None = _experiments_url()
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise CredentialVerificationError(
                    f"OCS rejected the API key (HTTP {resp.status_code})"
                )
            if not resp.is_success:
                raise CredentialVerificationError(
                    f"OCS API returned unexpected status {resp.status_code}"
                )
            payload = resp.json()
            results.extend(payload.get("results", []))
            url = payload.get("next")
    return results


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
        return [TenantDescriptor(str(e["id"]), e.get("name") or str(e["id"])) for e in experiments]

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        experiments = await _list_experiments(fields["api_key"])
        for e in experiments:
            if str(e["id"]) == external_id:
                return
        raise CredentialVerificationError(
            f"API key does not have access to experiment '{external_id}'"
        )
