"""Tests for the first-class TenantConnection model and OCS multi-team support."""

from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.users.models import Tenant, TenantConnection, TenantMembership


def _tenant(ext="exp-1"):
    return Tenant.objects.create(provider="ocs", external_id=ext, canonical_name=ext)


@pytest.mark.django_db
def test_connection_is_credential_only_and_links_memberships(user):
    conn = TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="enc",
    )
    tm = TenantMembership.objects.create(
        user=user, tenant=_tenant(), connection=conn, team_slug="acme", team_name="Acme"
    )
    assert tm.connection_id == conn.id
    assert list(conn.memberships.all()) == [tm]
    assert tm.archived_at is None


@pytest.mark.django_db
def test_one_oauth_connection_per_user_provider(user):
    TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    with pytest.raises(IntegrityError):
        TenantConnection.objects.create(
            user=user, provider="ocs", credential_type=TenantConnection.OAUTH
        )


@pytest.mark.django_db
def test_multiple_api_key_connections_allowed(user):
    TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="a",
    )
    TenantConnection.objects.create(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential="b",
    )
    assert TenantConnection.objects.filter(user=user, provider="ocs").count() == 2


@pytest.mark.django_db
def test_data_migration_maps_credentials(user):
    """forward() collapses OAuth creds per (user, provider) and links memberships."""
    import importlib

    from apps.users.models import TenantCredential

    t1, t2 = _tenant("exp-1"), _tenant("exp-2")
    tm1 = TenantMembership.objects.create(user=user, tenant=t1)
    tm2 = TenantMembership.objects.create(user=user, tenant=t2)
    TenantCredential.objects.create(tenant_membership=tm1, credential_type="oauth")
    TenantCredential.objects.create(tenant_membership=tm2, credential_type="oauth")

    mod = importlib.import_module("apps.users.migrations.0007_migrate_credentials_to_connections")
    from django.apps import apps as global_apps

    mod.forward(global_apps, None)

    tm1.refresh_from_db()
    tm2.refresh_from_db()
    assert tm1.connection is not None
    assert tm2.connection is not None
    # both OAuth memberships collapse into ONE connection per (user, provider)
    assert tm1.connection_id == tm2.connection_id
    assert tm1.connection.credential_type == "oauth"
