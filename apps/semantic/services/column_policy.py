"""Which generated semantic fields the catalog lists (#359).

Unlisted fields keep ``is_visible=True``: that flag means "exists in Cube", and
generated joins, curated measures, artifacts and explicit ``semantic_query``
members all resolve through it. The policy only keeps noise out of the dataset
browser and the agent's discovery tools.
"""

from __future__ import annotations

from typing import Any

HIDDEN_METADATA_KEY = "catalog_hidden"
TENANT_CONSTANT_ID = "tenant_constant_id"
RAW_JSON = "raw_json"

# The column carrying the tenant's own upstream id: in a single-tenant source
# table it repeats the same value on every row.
TENANT_CONSTANT_COLUMNS: dict[str, frozenset[str]] = {
    "commcare_connect": frozenset({"opportunity_id"}),
    "ocs": frozenset({"experiment_id"}),
}
_JSON_TYPES = frozenset({"json", "jsonb"})


def column_hidden_reason(provider: str, column_name: str, data_type: str) -> str:
    """Return why a physical column is left out of the catalog, or ``""`` to list it.

    ``provider`` is the single tenant that owns the table; pass ``""`` when the
    owner is unknown or several tenants share it, since the upstream id then
    distinguishes rows and is not constant.
    """
    if data_type.lower() in _JSON_TYPES:
        return RAW_JSON
    if column_name in TENANT_CONSTANT_COLUMNS.get(provider, frozenset()):
        return TENANT_CONSTANT_ID
    return ""


def is_listed(field: Any) -> bool:
    """Whether a semantic field belongs in catalog and discovery listings."""
    return field.is_visible and not (field.metadata or {}).get(HIDDEN_METADATA_KEY)
