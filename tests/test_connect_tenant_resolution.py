import pytest

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.tenant_resolution import ConnectAuthError, resolve_connect_opportunities


@pytest.mark.django_db(transaction=True)
class TestResolveConnectOpportunities:
    @pytest.mark.asyncio
    async def test_fetches_and_stores_opportunities(self, user, httpx_mock):
        httpx_mock.add_response(
            json={
                "opportunities": [
                    {"id": 42, "name": "Opp 42"},
                    {"id": 99, "name": "Test Opp"},
                ],
            },
        )

        memberships = await resolve_connect_opportunities(user, "fake-token")

        assert len(memberships) == 2
        assert memberships[0].tenant.provider == "commcare_connect"
        assert memberships[0].tenant.external_id == "42"
        assert memberships[0].tenant.canonical_name == "Opp 42"
        assert memberships[1].tenant.external_id == "99"
        assert memberships[1].tenant.canonical_name == "Test Opp"

        assert (
            await TenantMembership.objects.filter(
                user=user, tenant__provider="commcare_connect"
            ).acount()
            == 2
        )

        # The resolver creates ONE shared oauth TenantConnection per
        # (user, provider) and links every membership to it.
        oauth_conn = await TenantConnection.objects.aget(
            user=user,
            provider="commcare_connect",
            credential_type=TenantConnection.OAUTH,
        )

        async for tm in TenantMembership.objects.filter(
            user=user, tenant__provider="commcare_connect"
        ).select_related("connection"):
            assert tm.connection_id == oauth_conn.id

    @pytest.mark.asyncio
    async def test_updates_existing_opportunity_name(self, user, httpx_mock):
        tenant = await Tenant.objects.acreate(
            provider="commcare_connect", external_id="42", canonical_name="Old Name"
        )
        await TenantMembership.objects.acreate(user=user, tenant=tenant)

        httpx_mock.add_response(
            json={
                "opportunities": [
                    {"id": 42, "name": "New Name"},
                ],
            },
        )

        await resolve_connect_opportunities(user, "fake-token")

        await tenant.arefresh_from_db()
        assert tenant.canonical_name == "New Name"

    @pytest.mark.asyncio
    async def test_auth_error_raises(self, user, httpx_mock):
        httpx_mock.add_response(status_code=401)

        with pytest.raises(ConnectAuthError):
            await resolve_connect_opportunities(user, "fake-token")
