"""Row identity shared by every provider's list decoder."""

from __future__ import annotations

from typing import Any

from apps.users.services.tenant_listing.types import MalformedTenantList, TenantDescriptor

MAX_DESCRIPTION_CHARS = 2000


def clean_description(value: Any) -> str | None:
    """Provider text fit to store: Postgres rejects NUL and lone surrogates."""
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return text[:MAX_DESCRIPTION_CHARS] or None


def descriptor(row: Any, *, id_key: str, name_key: str) -> TenantDescriptor:
    """A tenant from one list row; a missing name falls back to the id, as ``Tenant.save`` does."""
    if not isinstance(row, dict):
        raise MalformedTenantList("list row is not an object")
    raw_id = row.get(id_key)
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        raise MalformedTenantList("list row has no usable id")
    external_id = str(raw_id).strip()
    if not external_id:
        raise MalformedTenantList("list row has an empty id")
    raw_name = row.get(name_key)
    if raw_name is not None and not isinstance(raw_name, str):
        raise MalformedTenantList("list row has a non-text name")
    # No list sends one yet (#917); read it so one that starts to is stored as-is.
    return TenantDescriptor(
        external_id, raw_name or external_id, description=clean_description(row.get("description"))
    )
