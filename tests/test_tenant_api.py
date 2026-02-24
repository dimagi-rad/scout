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


@pytest.mark.django_db
class TestTenantCredentialUpdateAPI:
    def test_patch_updates_credential(self, user):
        from apps.users.models import TenantCredential
        from apps.users.adapters import encrypt_credential

        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        TenantCredential.objects.create(
            tenant_membership=tm,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential=encrypt_credential("old@example.com:oldkey"),
        )

        client = Client()
        client.force_login(user)
        response = client.patch(
            f"/api/auth/tenant-credentials/{tm.id}/",
            data={
                "tenant_name": "Dimagi Updated",
                "credential": "new@example.com:newkey",
            },
            content_type="application/json",
        )
        assert response.status_code == 200
        tm.refresh_from_db()
        assert tm.tenant_name == "Dimagi Updated"
        tm.credential.refresh_from_db()
        assert tm.credential.encrypted_credential != encrypt_credential("old@example.com:oldkey")

    def test_patch_requires_auth(self):
        client = Client()
        response = client.patch(
            "/api/auth/tenant-credentials/00000000-0000-0000-0000-000000000000/",
            data={"tenant_name": "x"},
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_patch_returns_404_for_wrong_user(self, user, other_user):
        tm = TenantMembership.objects.create(
            user=other_user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        client = Client()
        client.force_login(user)
        response = client.patch(
            f"/api/auth/tenant-credentials/{tm.id}/",
            data={"tenant_name": "hijacked"},
            content_type="application/json",
        )
        assert response.status_code == 404
