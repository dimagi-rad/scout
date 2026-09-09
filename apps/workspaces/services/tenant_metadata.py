"""The one deterministic, live-only read scope for ``TenantMetadata`` (arch 09#7).

``TenantMetadata`` hangs off ``TenantMembership`` (OneToOne) but the data it holds
— app definitions, case types, form structure discovered during DISCOVER — is a
property of the *tenant*, not of the member who happened to trigger discovery.
``materialize_workspace`` fans out over every live membership of a tenant, so a
tenant with N members accumulates N rows, each written from that member's own
credential at its own time. Reading one of those with an unordered ``.first()``
answered "what is in this tenant's schema?" differently per request.

The rule: **the most-recently-discovered LIVE membership's metadata.**

- Live-only is a correctness fix, not a tidy-up: a related-field join does NOT
  apply ``TenantMembership``'s live-only default manager, so an *archived*
  membership — a tombstone for access the provider revoked (#249) — kept feeding
  its metadata into agent prompts, MCP ``describe_table``/``get_metadata``, the
  semantic catalog and the data dictionary. The ``archived_at IS NULL`` predicate
  is spelled out here because the join can't inherit it.
- ``discovered_at`` descending, NULLs last, so a row that actually fetched
  something outranks one that never did. ``pk`` is the tiebreak (a
  ``BigAutoField``, so it resolves to the last-inserted row), present only so
  equal ``discovered_at`` still yields one fixed answer across users, surfaces
  and requests.

The grain itself is still wrong; moving these fields onto ``Tenant`` is #305, and
this module's ordering is the winner rule that migration's dedupe should reuse.
"""

from __future__ import annotations

from django.db.models import F

from apps.workspaces.models import TenantMetadata


def winner_first(queryset):
    """Order a ``TenantMetadata`` queryset so the row this module serves comes first.

    Factored out so ``workspaces.0008``'s dedupe keeps *exactly* the row the read
    already answers with — one definition, so a change to the rule under review
    moves the migration with it. The live-only predicate is applied by the caller
    because a historical model in a migration has no live-only manager either.
    """
    return queryset.order_by(F("discovered_at").desc(nulls_last=True), "-pk")


def _live_metadata_qs(tenant_id):
    return winner_first(
        TenantMetadata.objects.filter(
            tenant_membership__tenant_id=tenant_id,
            tenant_membership__archived_at__isnull=True,
        )
    )


def get_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """Most-recently-discovered live ``TenantMetadata`` for a tenant, or ``None``."""
    return _live_metadata_qs(tenant_id).first()


async def aget_tenant_metadata(tenant_id) -> TenantMetadata | None:
    """Async sibling of :func:`get_tenant_metadata`."""
    return await _live_metadata_qs(tenant_id).afirst()
