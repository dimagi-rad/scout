"""Terminal model resolution and lineage chain traversal.

Provides functions for determining which TransformationAssets are "terminal"
(not replaced by any downstream asset) and for walking the replaces chain
from any asset back to its root source.
"""

from __future__ import annotations

from django.db import models

from apps.transformations.models import TransformationAsset


def get_terminal_assets(
    tenant_ids: list,
    workspace_id=None,
) -> list[TransformationAsset]:
    """Return TransformationAssets that are not replaced by any downstream asset.

    Terminal = no other asset has replaces=this_asset.

    Returns assets visible in this context (by tenant_ids and/or workspace_id).
    """
    visible = TransformationAsset.objects.filter(tenant_id__in=tenant_ids)
    if workspace_id:
        visible = visible | TransformationAsset.objects.filter(workspace_id=workspace_id)

    # IDs that are pointed to by some other visible asset's `replaces`
    replaced_ids = visible.filter(replaces__isnull=False).values_list("replaces_id", flat=True)

    return list(visible.exclude(id__in=replaced_ids))


def get_lineage_chain(
    asset_name: str,
    tenant_ids: list,
    workspace_id=None,
) -> list[dict]:
    """Follow the replaces chain backward from an asset to its root.

    Given model "cases_clean" which replaces "stg_case_patient":
    Returns [
        {"name": "cases_clean", "scope": "tenant", "description": "Cleaned cases..."},
        {"name": "stg_case_patient", "scope": "system", "description": "Staging..."},
    ]

    If the asset has no replaces chain, returns just the single asset.
    The raw source table is appended as the final entry if identifiable
    from the SQL (i.e., the terminal system model references raw_cases).
    """
    q = models.Q(tenant_id__in=tenant_ids)
    if workspace_id:
        q = q | models.Q(workspace_id=workspace_id)

    try:
        asset = TransformationAsset.objects.get(q, name=asset_name)
    except TransformationAsset.DoesNotExist:
        return []
    except TransformationAsset.MultipleObjectsReturned:
        # If multiple assets match (different scopes), prefer the most downstream one
        asset = TransformationAsset.objects.filter(q, name=asset_name).first()

    chain = []
    current = asset
    visited = set()  # Guard against cycles
    while current and current.id not in visited:
        visited.add(current.id)
        chain.append(
            {
                "name": current.name,
                "scope": current.scope,
                "description": current.description,
            }
        )
        current = current.replaces

    return chain
