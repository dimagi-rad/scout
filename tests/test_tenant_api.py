from unittest.mock import patch

import pytest
from django.test import Client

from apps.users.models import TenantMembership
from apps.users.services.tenant_verification import CommCareVerificationError


def _make_membership(user, external_id="dimagi", canonical_name="Dimagi", provider="commcare"):
    from apps.users.models import Tenant

    tenant = Tenant.objects.create(
        provider=provider, external_id=external_id, canonical_name=canonical_name
    )
    return TenantMembership.objects.create(user=user, tenant=tenant)


@pytest.mark.django_db
class TestTenantCredentialDeleteAPI:
    def test_delete_removes_domain_from_tenants_list(self, user):
        """Deleting a credential should remove the domain from GET /api/auth/tenants/."""
        from apps.users.adapters import encrypt_credential
        from apps.users.models import TenantCredential

        tm = _make_membership(user, external_id="sk-test", canonical_name="sk-test")
        TenantCredential.objects.create(
            tenant_membership=tm,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential=encrypt_credential("user@example.com:apikey"),
        )

        client = Client()
        client.force_login(user)

        # Confirm it appears in the tenants list before deletion
        response = client.get("/api/auth/tenants/")
        assert response.status_code == 200
        tenant_ids = [t["tenant_id"] for t in response.json()]
        assert "sk-test" in tenant_ids

        # Delete it
        response = client.delete(f"/api/auth/tenant-credentials/{tm.id}/")
        assert response.status_code == 200

        # Should no longer appear in the tenants list
        response = client.get("/api/auth/tenants/")
        assert response.status_code == 200
        tenant_ids = [t["tenant_id"] for t in response.json()]
        assert "sk-test" not in tenant_ids


@pytest.mark.django_db
class TestTenantListAPI:
    def test_list_tenants(self, user):
        _make_membership(user)
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
        tm = _make_membership(user)
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
        from apps.users.adapters import encrypt_credential
        from apps.users.models import TenantCredential

        tm = _make_membership(user)
        old_encrypted = encrypt_credential("old@example.com:oldkey")
        TenantCredential.objects.create(
            tenant_membership=tm,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential=old_encrypted,
        )

        client = Client()
        client.force_login(user)
        with patch(
            "apps.users.views.verify_commcare_credential",
            return_value={},
        ):
            response = client.patch(
                f"/api/auth/tenant-credentials/{tm.id}/",
                data={"credential": "new@example.com:newkey"},
                content_type="application/json",
            )
        assert response.status_code == 200
        # canonical_name on the shared Tenant is NOT changed by PATCH
        tm.tenant.refresh_from_db()
        assert tm.tenant.canonical_name == "Dimagi"
        tm.credential.refresh_from_db()
        assert tm.credential.encrypted_credential != old_encrypted

    def test_patch_rejects_unverified_credential(self, user):
        """PATCH must call verify_commcare_credential; invalid key is rejected."""
        from apps.users.models import TenantCredential

        tm = _make_membership(user)
        TenantCredential.objects.create(
            tenant_membership=tm,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential="x",
        )

        client = Client()
        client.force_login(user)
        with patch(
            "apps.users.views.verify_commcare_credential",
            side_effect=CommCareVerificationError("Bad key"),
        ):
            response = client.patch(
                f"/api/auth/tenant-credentials/{tm.id}/",
                data={"credential": "bad@evil.com:badkey"},
                content_type="application/json",
            )
        assert response.status_code == 400

    def test_patch_requires_auth(self):
        client = Client()
        response = client.patch(
            "/api/auth/tenant-credentials/00000000-0000-0000-0000-000000000000/",
            data={"credential": "x:y"},
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_patch_returns_404_for_wrong_user(self, user, other_user):
        tm = _make_membership(other_user)
        client = Client()
        client.force_login(user)
        with patch("apps.users.views.verify_commcare_credential", return_value={}):
            response = client.patch(
                f"/api/auth/tenant-credentials/{tm.id}/",
                data={"credential": "a:b"},
                content_type="application/json",
            )
        assert response.status_code == 404


@pytest.mark.django_db
class TestTenantCredentialCreateAPI:
    def test_create_with_valid_credential(self, user):
        """Valid credential creates Tenant + TenantMembership."""
        from apps.users.models import Tenant, TenantMembership

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.verify_commcare_credential",
            return_value={"domain": "dimagi", "username": "user@dimagi.org"},
        ):
            response = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "tenant_id": "dimagi",
                    "tenant_name": "Dimagi",
                    "credential": "user@dimagi.org:apikey123",
                },
                content_type="application/json",
            )

        assert response.status_code == 201
        assert Tenant.objects.filter(provider="commcare", external_id="dimagi").exists()
        assert TenantMembership.objects.filter(user=user, tenant__external_id="dimagi").exists()

    def test_create_with_invalid_credential_is_rejected(self, user):
        """Invalid credential must not create any records."""
        from apps.users.models import Tenant, TenantMembership

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.verify_commcare_credential",
            side_effect=CommCareVerificationError("Invalid"),
        ):
            response = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "tenant_id": "victim-domain",
                    "tenant_name": "Victim",
                    "credential": "attacker@evil.com:badkey",
                },
                content_type="application/json",
            )

        assert response.status_code == 400
        assert not Tenant.objects.filter(external_id="victim-domain").exists()
        assert not TenantMembership.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_tenant_list_includes_uuid(user, client):
    """GET /api/auth/tenants/ response includes internal tenant UUID."""
    tm = _make_membership(user, external_id="uuid-test", canonical_name="UUID Test")
    client.force_login(user)
    response = client.get("/api/auth/tenants/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    entry = next(e for e in data if e["tenant_id"] == "uuid-test")
    assert "tenant_uuid" in entry
    assert entry["tenant_uuid"] == str(tm.tenant.id)


@pytest.mark.django_db
class TestTenantCrossAccessAPI:
    def test_cross_tenant_access_blocked_by_structure(self, user, other_user):
        """A user who guesses another tenant's external_id cannot gain access
        because they cannot create a TenantMembership without a verified Tenant."""
        from apps.users.models import Tenant, TenantMembership

        # Simulate victim tenant exists (created by other_user via OAuth)
        victim_tenant = Tenant.objects.create(
            provider="commcare", external_id="victim-domain", canonical_name="Victim"
        )
        TenantMembership.objects.create(user=other_user, tenant=victim_tenant)

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.verify_commcare_credential",
            side_effect=CommCareVerificationError("Invalid"),
        ):
            response = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "tenant_id": "victim-domain",
                    "tenant_name": "Victim",
                    "credential": "attacker@evil.com:wrongkey",
                },
                content_type="application/json",
            )

        assert response.status_code == 400
        assert not TenantMembership.objects.filter(user=user).exists()
