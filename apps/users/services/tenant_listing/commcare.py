"""CommCare HQ's user-domain list (Tastypie), on whichever HQ server issued the credential."""

from __future__ import annotations

from typing import Any

from apps.common.commcare_servers import COMMCARE_SERVERS
from apps.users.models import TenantConnection
from apps.users.services.tenant_listing.rows import descriptor
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    ProviderRequest,
    TenantListPage,
)


def list_request(server_key: str, credential_type: str, credential: str) -> ProviderRequest | None:
    """The first-page request, or None when the server or stored credential is unusable.

    ``credential`` is the stored form: an OAuth access token, or an API key packed
    as ``username:api_key``.
    """
    server = COMMCARE_SERVERS.get(server_key)
    if server is None:
        return None
    if credential_type == TenantConnection.OAUTH:
        authorization = f"Bearer {credential}"
    elif credential_type == TenantConnection.API_KEY:
        username, separator, api_key = credential.partition(":")
        if not separator or not username or not api_key:
            return None
        authorization = f"ApiKey {username}:{api_key}"
    else:
        return None
    return ProviderRequest(server.user_domains_url, {"Authorization": authorization})


def decode_page(payload: Any) -> TenantListPage:
    """One page; HQ answers a relative ``meta.next`` that the paginator resolves (#328)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("objects"), list):
        raise MalformedTenantList("CommCare page has no domain list")
    meta = payload.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise MalformedTenantList("CommCare pagination metadata is not an object")
    next_url = meta.get("next")
    if next_url is not None and (not isinstance(next_url, str) or not next_url):
        raise MalformedTenantList("CommCare next link is not a URL")
    tenants = tuple(
        descriptor(row, id_key="domain_name", name_key="project_name") for row in payload["objects"]
    )
    return TenantListPage(tenants, next_url, next_declared=_declares_end(meta, len(tenants)))


def _declares_end(meta: dict, row_count: int) -> bool:
    """Whether the page says where the list ends.

    HQ serves user_domains unpaginated (``DoesNothingPaginator``): ``meta`` holds only
    ``total_count``, never ``next``. That count matching the rows is the whole list;
    anything short of it is a truncation verification must not trust (#880).
    """
    if "next" in meta:
        return True
    total_count = meta.get("total_count")
    return type(total_count) is int and total_count == row_count
