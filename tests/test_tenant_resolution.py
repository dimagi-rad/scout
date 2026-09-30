import pytest

from apps.users.models import Tenant, TenantMembership
from apps.users.services.tenant_resolution import CommCareAuthError, resolve_commcare_domains


@pytest.mark.django_db(transaction=True)
class TestResolveCommcareDomains:
    @pytest.mark.asyncio
    async def test_fetches_and_stores_domains(self, user, httpx_mock):

        httpx_mock.add_response(
            url="https://www.commcarehq.org/api/user_domains/v1/",
            json={
                "meta": {"limit": 20, "offset": 0, "total_count": 2, "next": None},
                "objects": [
                    {"domain_name": "dimagi", "project_name": "Dimagi"},
                    {"domain_name": "test-project", "project_name": "Test Project"},
                ],
            },
        )

        memberships = await resolve_commcare_domains(user, "fake-token")

        assert len(memberships) == 2
        assert memberships[0].tenant.external_id == "dimagi"
        assert memberships[1].tenant.external_id == "test-project"

        assert await TenantMembership.objects.filter(user=user).acount() == 2

    @pytest.mark.asyncio
    async def test_updates_existing_memberships(self, user, httpx_mock):

        tenant = await Tenant.objects.acreate(
            provider="commcare", external_id="dimagi", canonical_name="Old Name"
        )
        await TenantMembership.objects.acreate(user=user, tenant=tenant)

        httpx_mock.add_response(
            url="https://www.commcarehq.org/api/user_domains/v1/",
            json={
                "meta": {"limit": 20, "offset": 0, "total_count": 1, "next": None},
                "objects": [{"domain_name": "dimagi", "project_name": "New Name"}],
            },
        )

        await resolve_commcare_domains(user, "fake-token")

        await tenant.arefresh_from_db()
        assert tenant.canonical_name == "New Name"

    @pytest.mark.asyncio
    async def test_auth_error_raises(self, user, httpx_mock):

        httpx_mock.add_response(
            url="https://www.commcarehq.org/api/user_domains/v1/",
            status_code=401,
        )

        with pytest.raises(CommCareAuthError):
            await resolve_commcare_domains(user, "fake-token")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_commcare_domain_without_project_name_falls_back(user, httpx_mock):
    httpx_mock.add_response(
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={
            "meta": {"limit": 20, "offset": 0, "total_count": 1, "next": None},
            "objects": [{"domain_name": "dimagi"}],
        },
    )

    memberships = await resolve_commcare_domains(user, "fake-token")

    assert [tm.tenant.canonical_name for tm in memberships] == ["dimagi"]
