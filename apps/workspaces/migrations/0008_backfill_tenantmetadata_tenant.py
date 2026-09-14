"""Step 2 of moving ``TenantMetadata`` to tenant grain (#305): backfill and dedupe.

``materialize_workspace`` fans out over every live membership of a tenant, so a
tenant with N members accumulates up to N metadata rows that can genuinely
disagree. This sets ``tenant_id`` from the membership, then collapses each
tenant's rows to one.

The winner ordering freezes PR #415's newest-discovery-first rule, preferring
live memberships so the retained row matches the metadata served before this
migration. Archived-only tenants retain their newest row in storage; the read
service hides it until the tenant has a live membership again. Membership removal
must not delete durable tenant metadata (#305).

Reverse repoints each surviving row at the tenant's newest live membership
(falling back to its newest archived one). Rows deleted forward are **not**
restored — inherent to a dedupe. A row whose tenant has no membership at all
cannot be represented at membership grain and is dropped on the way back.
"""

import logging

from django.db import migrations
from django.db.models import Count, F, OuterRef, Subquery

logger = logging.getLogger(__name__)


def winner_first(queryset):
    # Freeze #415's service ordering: future application edits must not change dedupe.
    return queryset.order_by(F("discovered_at").desc(nulls_last=True), "-pk")


def backfill_tenant(apps, schema_editor):
    TenantMetadata = apps.get_model("workspaces", "TenantMetadata")
    TenantMembership = apps.get_model("users", "TenantMembership")

    backfilled = TenantMetadata.objects.filter(tenant_membership__isnull=False).update(
        tenant_id=Subquery(
            TenantMembership.objects.filter(pk=OuterRef("tenant_membership_id")).values(
                "tenant_id"
            )[:1]
        )
    )
    logger.info("TenantMetadata: set tenant_id on %d row(s)", backfilled)

    # Pre-0007 memberships were required; never dedupe unexpected NULLs as one tenant.
    duplicate_tenant_ids = list(
        TenantMetadata.objects.filter(tenant_id__isnull=False)
        .values("tenant_id")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .values_list("tenant_id", flat=True)
    )

    deleted_total = 0
    for tenant_id in duplicate_tenant_ids:
        rows = TenantMetadata.objects.filter(tenant_id=tenant_id)
        winner = (
            winner_first(rows.filter(tenant_membership__archived_at__isnull=True)).first()
            or winner_first(rows).first()
        )
        losers = rows.exclude(pk=winner.pk)
        logger.info(
            "TenantMetadata dedupe for tenant %s: keeping pk=%s (discovered_at=%s), removing %s",
            tenant_id,
            winner.pk,
            winner.discovered_at,
            [(r.pk, str(r.discovered_at)) for r in losers],
        )
        deleted_total += losers.delete()[0]

    logger.info(
        "TenantMetadata dedupe: %d duplicate row(s) deleted across %d tenant(s)",
        deleted_total,
        len(duplicate_tenant_ids),
    )


def restore_tenant_membership(apps, schema_editor):
    TenantMetadata = apps.get_model("workspaces", "TenantMetadata")
    TenantMembership = apps.get_model("users", "TenantMembership")

    repointed = 0
    dropped = 0
    for metadata in TenantMetadata.objects.filter(tenant_id__isnull=False):
        memberships = TenantMembership.objects.filter(tenant_id=metadata.tenant_id).order_by(
            "-created_at"
        )
        membership = memberships.filter(archived_at__isnull=True).first() or memberships.first()
        if membership is None:
            metadata.delete()
            dropped += 1
            continue
        metadata.tenant_membership_id = membership.id
        metadata.save(update_fields=["tenant_membership"])
        repointed += 1

    logger.info(
        "TenantMetadata reverse: repointed %d row(s) to a membership, dropped %d "
        "row(s) whose tenant has no membership",
        repointed,
        dropped,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0007_tenantmetadata_add_tenant"),
    ]

    operations = [
        migrations.RunPython(backfill_tenant, restore_tenant_membership),
    ]
