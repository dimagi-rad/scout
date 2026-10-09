"""CommCare Connect's opportunity export, plus its cheap per-opportunity check.

The export is one unpaginated document; a paginated one would mean every
caller reads only part of it, so any pagination field is a malformed page.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
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

# Above this many opportunities, one full listing beats one request each. The
# listing takes 10s+ for a user with hundreds of opportunities, so this must stay
# above the largest Connect workspace (11 in prod on 2026-10-09).
MAX_SUBSET = 25

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
    organization_names = _names_by(payload.get("organizations"), "slug")
    program_names = _names_by(payload.get("programs"), "id")
    tenants = []
    for row in payload["opportunities"]:
        tenant = descriptor(row, id_key="id", name_key="name")
        attributes = _attributes(row, organization_names, program_names)
        tenants.append(tenant._replace(attributes=attributes))
    return TenantListPage(tuple(tenants), None)


def _names_by(entries: Any, key: str) -> dict[str, str]:
    if not isinstance(entries, list):
        return {}
    names = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ref, name = _ref(entry.get(key)), _text(entry.get("name"))
        if ref is not None and name is not None:
            names[ref] = name
    return names


def _text(value: Any) -> str | None:
    # Postgres jsonb rejects NUL and lone surrogates, and a failed tenant write
    # fails the whole resolution.
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 255:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _ref(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return _text(str(value))


def _iso(value: Any, parse: Callable[[str], datetime.date]) -> str | None:
    """``value`` in extended ISO form; Python also accepts basic and week forms JS can't read."""
    if not isinstance(value, str):
        return None
    try:
        return parse(value).isoformat()
    except ValueError:
        return None


def _aware_iso(value: Any) -> str | None:
    """JS reads an offset-less datetime as the viewer's local time, so one is dropped."""
    iso = _iso(value, datetime.datetime.fromisoformat)
    if iso is None or datetime.datetime.fromisoformat(iso).tzinfo is None:
        return None
    return iso


def _attributes(
    row: dict, organization_names: dict[str, str], program_names: dict[str, str]
) -> dict[str, Any]:
    """The row's filter hints; a field of the wrong type is dropped, never raised.

    These only decorate the tenant list, so a surprising export must not cost
    the user access to an opportunity they can otherwise see.
    """
    attributes: dict[str, Any] = {}
    for key in ("is_active", "is_test"):
        if isinstance(row.get(key), bool):
            attributes[key] = row[key]
    if "end_date" in row and row["end_date"] is None:
        attributes["end_date"] = None
    elif end_date := _iso(row.get("end_date"), datetime.date.fromisoformat):
        attributes["end_date"] = end_date
    if date_created := _aware_iso(row.get("date_created")):
        attributes["date_created"] = date_created
    organization = _text(row.get("organization"))
    if organization:
        attributes["organization"] = organization
        if organization in organization_names:
            attributes["organization_name"] = organization_names[organization]
    program = _ref(row.get("program"))
    if program:
        attributes["program"] = program
        if program in program_names:
            attributes["program_name"] = program_names[program]
    visit_count = row.get("visit_count")
    if isinstance(visit_count, int) and not isinstance(visit_count, bool):
        attributes["visit_count"] = visit_count
    return attributes


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
