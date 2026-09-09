"""The one read scope for ``TenantMetadata`` (arch 09#7, #305).

``TenantMetadata`` is one row per tenant, so the read is a lookup by key. The
module survives the move to tenant grain because five call sites — the data
dictionary, the semantic catalog (sync and async), MCP ``describe_table`` and MCP
``get_metadata`` — read through it, and because it was previously the only place
that got the read right: the rows used to hang off ``TenantMembership``, so an
unordered ``.first()`` over a tenant's N per-member rows answered "what is in
this tenant's schema?" differently per request, and a related-field join skipped
``TenantMembership``'s live-only manager, feeding metadata from *archived*
(upstream-revoked) memberships into all four surfaces.

Both of those are structural now: there is one row and it is keyed on the tenant.
A tenant nobody holds live access to is unreachable through every caller's own
authorization, so metadata deliberately outlives revocation of any one
membership — that was #305's wrong ``CASCADE``.
"""

from __future__ import annotations

from django.db.models import F

from apps.workspaces.models import TenantMetadata


def winner_first(queryset):
    """Order per-membership ``TenantMetadata`` rows the way the read used to pick one.

    Retained past the grain change because ``workspaces.0008``'s dedupe imports it:
    it is the recorded definition of which of a tenant's duplicate rows survived,
    kept as one definition so a change to the rule moved the migration with it.
    Delete alongside 0008.
    """
    return queryset.order_by(F("discovered_at").desc(nulls_last=True), "-pk")


def get_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """The tenant's ``TenantMetadata``, or ``None`` if discovery never ran."""
    return TenantMetadata.objects.filter(tenant_id=tenant_id).first()


async def aget_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """Async sibling of :func:`get_tenant_metadata`."""
    return await TenantMetadata.objects.filter(tenant_id=tenant_id).afirst()
