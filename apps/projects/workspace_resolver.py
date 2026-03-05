"""Shared workspace resolution for tenant-scoped API views."""

from rest_framework import status
from rest_framework.response import Response

from apps.projects.models import TenantWorkspace
from apps.users.models import TenantMembership


def resolve_workspace(request, tenant_id):
    """Resolve TenantWorkspace from the tenant_id URL path parameter.

    tenant_id is the TenantMembership.id (UUID) and must belong to
    request.user. Returns (workspace, None) on success or (None, Response(403))
    on error.
    """
    try:
        membership = TenantMembership.objects.select_related("tenant").get(
            id=tenant_id, user=request.user
        )
    except TenantMembership.DoesNotExist:
        return None, Response(
            {"error": "Tenant not found or access denied."},
            status=status.HTTP_403_FORBIDDEN,
        )
    workspace, _ = TenantWorkspace.objects.get_or_create(tenant=membership.tenant)
    return workspace, None
