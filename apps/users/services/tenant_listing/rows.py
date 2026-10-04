"""Row identity shared by every provider's list decoder."""

from __future__ import annotations

from typing import Any

from apps.users.services.tenant_listing.types import MalformedTenantList, TenantDescriptor


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
    return TenantDescriptor(external_id, raw_name or external_id)
