"""The shared canonical-provider comparison.

Publication canonicalizes independently of claims and result mapping, so a helper
that regresses to a SQL prefix match is not observable end-to-end. These tests pin
the comparison where it lives.
"""

import pytest

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.oauth_scope import (
    amemberships_on_provider,
    memberships_on_provider,
    same_provider,
)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("commcare", "commcare", True),
        ("commcare", "commcare-custom", True),
        ("commcare_connect", "commcare_connect_staging", True),
        ("ocs", "ocs-team", True),
        # commcare_connect starts with "commcare" but is a different provider.
        ("commcare", "commcare_connect", False),
        ("commcare_connect", "commcare", False),
        ("commcare", "ocs", False),
        ("other", "other", True),
        ("other", "other-alias", False),
    ],
)
def test_same_provider(left, right, expected):
    assert same_provider(left, right) is expected
    assert same_provider(right, left) is expected


@pytest.fixture
def mixed_memberships(user):
    connection = TenantConnection.objects.create(
        user=user, provider="commcare", credential_type=TenantConnection.API_KEY
    )
    tenants = {
        provider: Tenant.objects.create(
            provider=provider, external_id=f"{provider}-id", canonical_name=provider
        )
        for provider in ("commcare", "commcare-custom", "commcare_connect", "ocs")
    }
    for tenant in tenants.values():
        TenantMembership.objects.create(user=user, tenant=tenant, connection=connection)
    return TenantMembership.all_objects.filter(connection=connection), tenants


@pytest.mark.django_db
def test_memberships_on_provider_keeps_aliases_and_excludes_connect(mixed_memberships):
    memberships, tenants = mixed_memberships

    matched = memberships_on_provider(memberships, "commcare", "tenant_id")

    assert set(matched) == {tenants["commcare"].id, tenants["commcare-custom"].id}


@pytest.mark.django_db
def test_memberships_on_provider_connect_does_not_match_commcare(mixed_memberships):
    memberships, tenants = mixed_memberships

    assert memberships_on_provider(memberships, "commcare_connect", "tenant_id") == [
        tenants["commcare_connect"].id
    ]


@pytest.mark.django_db
def test_memberships_on_provider_multiple_fields_drop_provider_column(mixed_memberships):
    memberships, tenants = mixed_memberships

    rows = memberships_on_provider(memberships, "ocs", "tenant_id", "archived_at")

    assert rows == [(tenants["ocs"].id, None)]


def test_memberships_on_provider_requires_a_field():
    with pytest.raises(ValueError, match="at least one field is required"):
        memberships_on_provider(TenantMembership.all_objects.none(), "commcare")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_amemberships_on_provider_matches_sync(user):
    connection = await TenantConnection.objects.acreate(
        user=user, provider="commcare", credential_type=TenantConnection.API_KEY
    )
    alias = await Tenant.objects.acreate(
        provider="commcare-custom", external_id="alias", canonical_name="Alias"
    )
    connect = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="connect", canonical_name="Connect"
    )
    for tenant in (alias, connect):
        await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=connection)
    memberships = TenantMembership.all_objects.filter(connection=connection)

    assert await amemberships_on_provider(memberships, "commcare", "tenant_id") == [alias.id]
