"""Soft-delete manager on TenantMembership + the sites that must see tombstones.

An archived TenantMembership is a tombstone for upstream-revoked access. The
default ``objects`` manager hides it (so access reads are safe by default); the
``all_objects`` escape hatch is for writes/merges/resolution that must reconcile
tombstones. See docs/superpowers/specs/2026-06-18-tenant-access-refresh-design.md.
"""

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.merge import merge_users

User = get_user_model()


def _tenant(external_id="1", provider="commcare_connect"):
    return Tenant.objects.create(
        provider=provider, external_id=external_id, canonical_name=f"T{external_id}"
    )


@pytest.mark.django_db
def test_default_manager_hides_archived_all_objects_sees_it():
    u = User.objects.create(email="a@dimagi.com", username="a")
    t = _tenant()
    tm = TenantMembership.all_objects.create(user=u, tenant=t)

    assert TenantMembership.objects.filter(user=u).count() == 1
    tm.archived_at = timezone.now()
    tm.save(update_fields=["archived_at"])

    assert TenantMembership.objects.filter(user=u).count() == 0  # hidden by default
    assert TenantMembership.all_objects.filter(user=u).count() == 1  # escape hatch


@pytest.mark.django_db
def test_reverse_manager_hides_archived():
    # conn.memberships inherits the live-only default manager class.
    u = User.objects.create(email="b@dimagi.com", username="b")
    conn = TenantConnection.objects.create(
        user=u, provider="commcare_connect", credential_type=TenantConnection.OAUTH
    )
    t = _tenant("2")

    TenantMembership.all_objects.create(
        user=u, tenant=t, connection=conn, archived_at=timezone.now()
    )
    assert conn.memberships.count() == 0
    assert TenantMembership.all_objects.filter(connection=conn).count() == 1


@pytest.mark.django_db
def test_cascade_deletes_archived_rows():
    # base_manager_name=all_objects keeps cascade collection whole.

    u = User.objects.create(email="c@dimagi.com", username="c")
    t = _tenant("3")
    TenantMembership.all_objects.create(user=u, tenant=t, archived_at=timezone.now())
    t.delete()
    assert TenantMembership.all_objects.filter(tenant_id=t.id).count() == 0


@pytest.mark.django_db
def test_merge_live_beats_tombstone():
    # canonical holds a tombstone for a tenant; duplicate holds a live row.

    canonical = User.objects.create(email="dupe@dimagi.com", username="canon")
    duplicate = User.objects.create(username="dup")  # email-less OAuth account
    t = _tenant("4")
    TenantMembership.all_objects.create(user=canonical, tenant=t, archived_at=timezone.now())
    TenantMembership.all_objects.create(user=duplicate, tenant=t)  # live

    merge_users(canonical=canonical, duplicate=duplicate)

    survivors = TenantMembership.all_objects.filter(user=canonical, tenant=t)
    assert survivors.count() == 1
    assert survivors.first().archived_at is None  # live wins, tombstone dropped


@pytest.mark.django_db
def test_merge_keeps_canonical_when_it_is_live():
    canonical = User.objects.create(email="keep@dimagi.com", username="canon2")
    duplicate = User.objects.create(username="dup2")
    t = _tenant("5")
    TenantMembership.all_objects.create(user=canonical, tenant=t)  # live
    TenantMembership.all_objects.create(user=duplicate, tenant=t)  # live too → conflict

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert TenantMembership.all_objects.filter(user=canonical, tenant=t).count() == 1
    assert report.tenant_membership_conflict_deleted == 1
