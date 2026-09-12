"""``TenantMetadata`` must read the same for every user and every surface (arch 09#7).

The rows are per-membership and ``materialize_workspace`` writes one per live
member, so a tenant with N members has N rows that can genuinely disagree. These
tests pin the read rule — most-recently-discovered LIVE membership — and, in
particular, that a join to an archived (upstream-revoked) membership can no
longer feed the agent prompt, MCP, the semantic catalog or the data dictionary.
"""

from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.semantic.services.catalog import _tenant_metadata_for_schema
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import SchemaState, TenantMetadata, TenantSchema
from apps.workspaces.services.tenant_metadata import aget_tenant_metadata, get_tenant_metadata

SCHEMA_NAME = "t_md_domain"


@pytest.fixture
def md_tenant(db):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="md-domain", canonical_name="MD Domain"
    )
    TenantSchema.objects.create(tenant=tenant, schema_name=SCHEMA_NAME, state=SchemaState.ACTIVE)
    return tenant


def _metadata(tenant, owner, *, discovered_at=None, archived=False):
    """Give ``tenant`` one more member whose membership carries its own metadata."""
    user = get_user_model().objects.create_user(email=f"{owner}@example.com")
    tm = TenantMembership.all_objects.create(
        user=user,
        tenant=tenant,
        archived_at=timezone.now() if archived else None,
    )
    return TenantMetadata.objects.create(
        tenant_membership=tm,
        metadata={"owner": owner},
        discovered_at=discovered_at,
    )


def _every_surface(tenant):
    """Read the tenant's metadata through each surface; return their ``owner`` values.

    The sync service backs the DRF data dictionary, the async one backs MCP
    ``describe_table``/``get_metadata`` and the async catalog, and
    ``_tenant_metadata_for_schema`` is the semantic catalog's sync entry point.
    """
    reads = [
        get_tenant_metadata(tenant.id),
        async_to_sync(aget_tenant_metadata)(tenant.id),
        _tenant_metadata_for_schema(SCHEMA_NAME),
    ]
    return [None if md is None else md.metadata["owner"] for md in reads]


@pytest.mark.django_db
def test_every_surface_agrees_when_memberships_disagree(md_tenant):
    now = timezone.now()
    _metadata(md_tenant, "stale", discovered_at=now - timedelta(days=3))
    _metadata(md_tenant, "fresh", discovered_at=now)
    _metadata(md_tenant, "never", discovered_at=None)

    assert _every_surface(md_tenant) == ["fresh"] * 3


@pytest.mark.django_db
def test_archived_membership_metadata_is_never_returned(md_tenant):
    """The archived row is deliberately the *fresher* one, so recency alone can't
    save this: an ``archived_at`` predicate must be spelled out, because a
    related-field join does not inherit ``TenantMembership``'s live-only manager.
    """
    now = timezone.now()
    _metadata(md_tenant, "revoked", discovered_at=now, archived=True)
    _metadata(md_tenant, "live", discovered_at=now - timedelta(days=1))

    assert _every_surface(md_tenant) == ["live"] * 3


@pytest.mark.django_db
def test_only_archived_metadata_reads_as_absent(md_tenant):
    _metadata(md_tenant, "revoked", discovered_at=timezone.now(), archived=True)

    assert _every_surface(md_tenant) == [None] * 3


@pytest.mark.django_db
def test_undiscovered_row_loses_to_a_discovered_one_inserted_after_it(md_tenant):
    _metadata(md_tenant, "never", discovered_at=None)
    _metadata(md_tenant, "discovered", discovered_at=timezone.now())

    assert _every_surface(md_tenant) == ["discovered"] * 3


@pytest.mark.django_db
def test_tied_discovery_times_still_resolve_to_one_stable_answer(md_tenant):
    """Equal ``discovered_at`` must not leave the winner up to the query planner."""
    at = timezone.now()
    for i in range(4):
        _metadata(md_tenant, f"tie{i}", discovered_at=at)

    winners = {get_tenant_metadata(md_tenant.id).metadata["owner"] for _ in range(5)}
    assert len(winners) == 1
    assert set(_every_surface(md_tenant)) == winners
