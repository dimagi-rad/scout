"""CommCare Connect's opportunity export, plus its cheap per-opportunity check.

The export is one unpaginated document; a paginated one would mean every
caller reads only part of it, so any pagination field is a malformed page.
"""

from __future__ import annotations

from typing import Any

import httpx

from apps.users.models import TenantConnection
from apps.users.services.tenant_listing.rows import descriptor
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    ProviderRequest,
    TenantListPage,
)
from mcp_server.loaders._urls import ProviderURLPolicy

# Above this many opportunities, one full listing beats one request each.
MAX_SUBSET = 5

_PAGINATION_KEYS = frozenset(
    {
        "next",
        "previous",
        "pagination",
        "meta",
        "has_more",
        "next_page",
        "count",
        "page",
        "page_size",
        "limit",
        "offset",
    }
)


def list_request(base_url: str, credential_type: str, credential: str) -> ProviderRequest | None:
    if credential_type != TenantConnection.OAUTH:
        return None
    url = f"{base_url.rstrip('/')}/export/opp_org_program_list/"
    return ProviderRequest(url, {"Authorization": f"Bearer {credential}"})


def decode_page(payload: Any) -> TenantListPage:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("opportunities"), list)
        or _PAGINATION_KEYS.intersection(payload)
    ):
        raise MalformedTenantList("Connect response is incomplete or paginated")
    tenants = tuple(
        descriptor(row, id_key="id", name_key="name") for row in payload["opportunities"]
    )
    return TenantListPage(tenants, None)


def subset_ids(external_ids) -> tuple[str, ...] | None:
    """The opportunity ids to check one by one, or None to use the full listing.

    ``/export/opp_org_program_list/`` exports every opportunity with a per-row
    visit count and takes ~10s for users with many opportunities, which alone
    exhausts the interactive budget. ``/export/opportunity/<id>/`` answers the
    question for one opportunity cheaply.
    """
    if not external_ids or len(external_ids) > MAX_SUBSET:
        return None
    ids = tuple(sorted(external_ids))
    # Connect routes ``<int:opp_id>``; any other id 404s at routing, which must
    # never be read as a denial, and a zero-padded one comes back renumbered.
    if not all(
        external_id.isascii() and external_id.isdigit() and str(int(external_id)) == external_id
        for external_id in ids
    ):
        return None
    return ids


def verify_subset_request(listing: ProviderRequest, external_id: str) -> ProviderRequest:
    """The one-opportunity request beside ``listing``; raises UnsafeProviderURL off-origin."""
    url = ProviderURLPolicy(listing.url).resolve(
        f"../opportunity/{external_id}/", relative_to=listing.url
    )
    return ProviderRequest(url, listing.headers)


def is_no_access_answer(response: httpx.Response) -> bool:
    """DRF's NotFound body, as opposed to a routing 404 (an HTML page) or a proxy's.

    Connect answers an opportunity the user may not export via
    ``_get_opportunity_or_404`` with ``{"detail": "Not found."}``. ``detail`` is
    localized, so only the shape is checked.
    """
    if response.status_code != 404:
        return False
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and set(payload) == {"detail"}
        and isinstance(payload["detail"], str)
    )


def confirms_opportunity(response: httpx.Response, external_id: str) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    raw_id = payload.get("id")
    return (
        not isinstance(raw_id, bool)
        and isinstance(raw_id, (int, str))
        and str(raw_id) == external_id
    )
