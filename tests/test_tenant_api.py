import pytest
from django.test import Client

from apps.users.models import TenantMembership


@pytest.mark.django_db
class TestTenantListAPI:
    def test_list_tenants(self, user):
        TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        client = Client()
        client.force_login(user)
        response = client.get("/api/auth/tenants/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["tenant_id"] == "dimagi"

    def test_unauthenticated(self):
        client = Client()
        response = client.get("/api/auth/tenants/")
        assert response.status_code == 401


@pytest.mark.django_db
class TestTenantSelectAPI:
    def test_select_tenant(self, user):
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        client = Client()
        client.force_login(user)
        response = client.post(
            "/api/auth/tenants/select/",
            data={"tenant_id": str(tm.id)},
            content_type="application/json",
        )
        assert response.status_code == 200
        tm.refresh_from_db()
        assert tm.last_selected_at is not None
