"""Read tenant-owned metadata only while the tenant has a live membership.

Storage survives membership revocation and deletion (#305), but visibility keeps
#415's any-live-membership gate. Workspace authorization is separate: it can admit
a caller through another tenant in a multi-tenant workspace. This is not a check
of the caller's own membership in the tenant whose metadata is being read.
"""

from __future__ import annotations

from apps.users.models import TenantMembership
from apps.workspaces.models import TenantMetadata


def _visible_metadata(tenant_id):
    # Keep #415's visibility rule explicit even if the default manager changes.
    return TenantMetadata.objects.filter(
        tenant_id=tenant_id,
        tenant_id__in=TenantMembership.objects.filter(
            tenant_id=tenant_id, archived_at__isnull=True
        ).values("tenant_id"),
    )


def get_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """Return metadata if discovered and at least one tenant membership is live."""
    return _visible_metadata(tenant_id).first()


async def aget_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """Async sibling of :func:`get_tenant_metadata`."""
    return await _visible_metadata(tenant_id).afirst()
