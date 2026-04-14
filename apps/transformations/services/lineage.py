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
    """
    q = models.Q(tenant_id__in=tenant_ids)
    if workspace_id:
        q = q | models.Q(workspace_id=workspace_id)

    try:
        asset = TransformationAsset.objects.get(q, name=asset_name)
    except TransformationAsset.DoesNotExist:
        return []
    except TransformationAsset.MultipleObjectsReturned:
        # If multiple assets match (different scopes), prefer the most downstream one.
        # Default ordering is ["scope", "name"] which sorts system < tenant < workspace,
        # so reverse to get workspace (most downstream) first.
        asset = TransformationAsset.objects.filter(q, name=asset_name).order_by("-scope").first()

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
        # Follow replaces chain scoped to visible assets only, preventing
        # cross-tenant information disclosure via unscoped FK traversal.
        next_id = current.replaces_id
        if next_id is None:
            break
        current = TransformationAsset.objects.filter(q, id=next_id).first()

    return chain


async def aget_terminal_assets(
    tenant_ids: list,
    workspace_id=None,
) -> list[TransformationAsset]:
    """Async version of get_terminal_assets."""
    visible = TransformationAsset.objects.filter(tenant_id__in=tenant_ids)
    if workspace_id:
        visible = visible | TransformationAsset.objects.filter(workspace_id=workspace_id)

    replaced_ids = visible.filter(replaces__isnull=False).values_list("replaces_id", flat=True)

    return [asset async for asset in visible.exclude(id__in=replaced_ids)]


async def aget_lineage_chain(
    asset_name: str,
    tenant_ids: list,
    workspace_id=None,
) -> list[dict]:
    """Async version of get_lineage_chain."""
    q = models.Q(tenant_id__in=tenant_ids)
    if workspace_id:
        q = q | models.Q(workspace_id=workspace_id)

    try:
        asset = await TransformationAsset.objects.aget(q, name=asset_name)
    except TransformationAsset.DoesNotExist:
        return []
    except TransformationAsset.MultipleObjectsReturned:
        asset = (
            await TransformationAsset.objects.filter(q, name=asset_name).order_by("-scope").afirst()
        )

    chain = []
    current = asset
    visited = set()
    while current and current.id not in visited:
        visited.add(current.id)
        chain.append(
            {
                "name": current.name,
                "scope": current.scope,
                "description": current.description,
            }
        )
        next_id = current.replaces_id
        if next_id is None:
            break
        current = await TransformationAsset.objects.filter(q, id=next_id).afirst()

    return chain
