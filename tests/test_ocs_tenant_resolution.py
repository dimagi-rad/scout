"""Tests for OCS tenant resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from apps.users.models import Tenant, TenantCredential, TenantMembership
from apps.users.services.tenant_resolution import OCSAuthError, resolve_ocs_chatbots


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_ocs_chatbots_creates_tenants(user):
    experiments = [
        {
            "id": "exp-uuid-1",
            "name": "Onboarding Bot",
            "url": "https://example/api/experiments/exp-uuid-1/",
            "version_number": 1,
        },
        {
            "id": "exp-uuid-2",
            "name": "Survey Bot",
            "url": "https://example/api/experiments/exp-uuid-2/",
            "version_number": 2,
        },
    ]

    async def fake_get(*args, **kwargs):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"results": experiments, "next": None}

        return R()

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=fake_get)
        memberships = await resolve_ocs_chatbots(user, "access-tok")

    assert len(memberships) == 2
    tenants = [t async for t in Tenant.objects.filter(provider="ocs").order_by("external_id")]
    assert [t.external_id for t in tenants] == ["exp-uuid-1", "exp-uuid-2"]
    assert [t.canonical_name for t in tenants] == ["Onboarding Bot", "Survey Bot"]

    creds = await TenantCredential.objects.filter(
        tenant_membership__tenant__provider="ocs"
    ).acount()
    assert creds == 2

    # Ensure memberships belong to the user and are for OCS tenants
    assert all(tm.user_id == user.id for tm in memberships)
    assert all(tm.tenant.provider == "ocs" for tm in memberships)

    # TenantMembership count
    tm_count = await TenantMembership.objects.filter(user=user, tenant__provider="ocs").acount()
    assert tm_count == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_ocs_chatbots_raises_on_auth_failure(user):
    async def fake_get(*args, **kwargs):
        class R:
            status_code = 401

            def raise_for_status(self):
                pass

            def json(self):
                return {}

        return R()

    with patch("httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=fake_get)
        with pytest.raises(OCSAuthError):
            await resolve_ocs_chatbots(user, "bad-tok")
