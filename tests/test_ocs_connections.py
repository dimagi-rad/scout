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
