"""``workspaces.0008`` must keep the row the read path already serves (#305).

The dedupe deletes rows, so its frozen winner rule has to match the former read
path (PR #415) — otherwise the deploy silently swaps which
metadata a tenant sees and the evidence is gone. These tests run the real
migration against production-shaped data: a tenant with three memberships whose
metadata disagrees, one of them archived and holding the freshest row.

The rewind/replay is guarded by ``try/finally`` so a failure here cannot leave the
test database on an older schema for the rest of the session.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership, User

BEFORE = ("workspaces", "0007_tenantmetadata_add_tenant")
AFTER = ("workspaces", "0008_backfill_tenantmetadata_tenant")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    executor.loader.build_graph()
    return executor


def _leaf():
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return next(n for n in executor.loader.graph.leaf_nodes("workspaces"))


def _historical_metadata_model(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps.get_model("workspaces", "TenantMetadata")


def _membership(tenant, suffix, *, archived=False):
    user = User.objects.create_user(email=f"m{suffix}@example.com", password="x")
    return TenantMembership.all_objects.create(
        user=user,
        tenant=tenant,
        archived_at=timezone.now() if archived else None,
    )


@pytest.fixture
def production_shaped_metadata(transactional_db):
    """Seed at the pre-backfill schema and yield the historical model + expectations."""
    leaf = _leaf()
    _migrate(BEFORE)
    Metadata = _historical_metadata_model(BEFORE)

    now = timezone.now()
    contested = Tenant.objects.create(
        provider="commcare", external_id="contested", canonical_name="Contested"
    )
    stale_live = _membership(contested, "stale")
    fresh_live = _membership(contested, "fresh")
    archived = _membership(contested, "archived", archived=True)

    Metadata.objects.create(
        tenant_membership_id=stale_live.id,
        metadata={"owner": "stale-live"},
        discovered_at=now - timedelta(days=2),
    )
    winner = Metadata.objects.create(
        tenant_membership_id=fresh_live.id,
        metadata={"owner": "fresh-live"},
        discovered_at=now - timedelta(days=1),
    )
    # Freshest row of the three, but on a revoked membership: it must lose.
    Metadata.objects.create(
        tenant_membership_id=archived.id,
        metadata={"owner": "archived"},
        discovered_at=now,
    )

    revoked = Tenant.objects.create(
        provider="commcare", external_id="revoked", canonical_name="Revoked"
    )
    revoked_only = _membership(revoked, "revoked", archived=True)
    Metadata.objects.create(
        tenant_membership_id=revoked_only.id,
        metadata={"owner": "revoked-only"},
        discovered_at=now,
    )

    single = Tenant.objects.create(
        provider="commcare", external_id="single", canonical_name="Single"
    )
    single_m = _membership(single, "single")
    Metadata.objects.create(tenant_membership_id=single_m.id, metadata={"owner": "single"})

    try:
        yield {
            "model": Metadata,
            "contested": contested,
            "revoked": revoked,
            "single": single,
            "winner_pk": winner.pk,
            "fresh_live": fresh_live,
        }
    finally:
        _migrate(leaf)


def test_dedupe_keeps_the_row_the_read_rule_serves(production_shaped_metadata, caplog):
    seed = production_shaped_metadata
    with caplog.at_level("INFO", logger="apps.workspaces.migrations"):
        _migrate(AFTER)
    Metadata = _historical_metadata_model(AFTER)

    survivors = Metadata.objects.filter(tenant_id=seed["contested"].id)
    assert survivors.count() == 1
    survivor = survivors.get()
    assert survivor.pk == seed["winner_pk"]
    assert survivor.metadata["owner"] == "fresh-live"
    assert survivor.tenant_id == seed["contested"].id

    assert "2 duplicate row(s) deleted across 1 tenant(s)" in caplog.text

    # Tenants with nothing to dedupe are only backfilled, never touched.
    assert Metadata.objects.get(tenant_id=seed["single"].id).metadata["owner"] == "single"
    assert Metadata.objects.get(tenant_id=seed["revoked"].id).metadata["owner"] == "revoked-only"


def test_reverse_repoints_survivors_at_the_newest_live_membership(production_shaped_metadata):
    seed = production_shaped_metadata
    _migrate(AFTER)
    _migrate(BEFORE)

    Metadata = _historical_metadata_model(BEFORE)
    restored = Metadata.objects.get(tenant_membership__tenant_id=seed["contested"].id)
    assert restored.metadata["owner"] == "fresh-live"
    assert restored.tenant_membership_id == seed["fresh_live"].id
    # A dedupe cannot resurrect the rows it deleted.
    assert Metadata.objects.count() == 3
