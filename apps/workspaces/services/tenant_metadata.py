"""One deterministic TenantMetadata read scope (arch #251, Phase 4, Decision 5).

``TenantMetadata`` is a per-membership row (OneToOne to ``TenantMembership``),
written by the materializer to the *triggering user's* membership only. Reading
it per-user (or off an arbitrary membership) made the same tenant/table show
different column annotations per user and per surface. This module is the ONE
read every surface (agent prompt, MCP ``describe_table``/``get_metadata``, DRF
data dictionary) uses so they always agree.

The rule: **the most-recently-discovered LIVE membership for the tenant.**

- Archived (upstream-revoked, #249) memberships are tombstones and must never
  leak annotations into a prompt — a related-field join does NOT apply
  ``TenantMembership``'s live-only default manager, so the ``archived_at IS
  NULL`` predicate is spelled out here.
- "Most-recently-discovered" orders by the discovery timestamp
  (``discovered_at``, NULLs last so a membership that actually discovered
  metadata outranks one that never did), with a stable ``pk`` tiebreak so the
  result is deterministic across users and surfaces.

This does NOT migrate the model to per-tenant (a noted follow-up); it only makes
the READ deterministic and live-filtered.
"""

from __future__ import annotations

from django.db.models import F

from apps.workspaces.models import TenantMetadata


def _live_metadata_qs(tenant_id):
    return TenantMetadata.objects.filter(
        tenant_membership__tenant_id=tenant_id,
        tenant_membership__archived_at__isnull=True,
    ).order_by(F("discovered_at").desc(nulls_last=True), "-pk")


async def aget_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """Async: most-recently-discovered live TenantMetadata for a tenant, or None."""
    return await _live_metadata_qs(tenant_id).afirst()


def get_tenant_metadata_sync(tenant_id) -> TenantMetadata | None:
    """Sync sibling of ``aget_tenant_metadata`` for the DRF data dictionary."""
    return _live_metadata_qs(tenant_id).first()
