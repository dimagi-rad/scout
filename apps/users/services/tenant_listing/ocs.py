"""Open Chat Studio's experiment list (DRF pagination), for an OAuth team or an API key."""

from __future__ import annotations

from typing import Any

from apps.users.models import TenantConnection
from apps.users.services.tenant_listing.rows import descriptor
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    ProviderRequest,
    TenantListPage,
)


def list_request(base_url: str, credential_type: str, credential: str) -> ProviderRequest | None:
    """The first-page request, or None for a credential type OCS does not take."""
    url = f"{base_url.rstrip('/')}/api/experiments/"
    if credential_type == TenantConnection.OAUTH:
        return ProviderRequest(url, {"Authorization": f"Bearer {credential}"})
    if credential_type == TenantConnection.API_KEY:
        return ProviderRequest(url, {"X-api-key": credential})
    return None


def decode_page(payload: Any) -> TenantListPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise MalformedTenantList("OCS page has no experiment list")
    next_url = payload.get("next")
    if next_url is not None and (not isinstance(next_url, str) or not next_url):
        raise MalformedTenantList("OCS next link is not a URL")
    tenants = tuple(descriptor(row, id_key="id", name_key="name") for row in payload["results"])
    return TenantListPage(tenants, next_url, next_declared="next" in payload)
