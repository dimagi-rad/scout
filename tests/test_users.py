import pytest
from django.contrib.auth import get_user_model

from apps.users.models import TenantCredential, TenantMembership

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="dev@example.com", password="pass1234")


@pytest.fixture
def membership(user):
    return TenantMembership.objects.create(
        user=user,
        provider="commcare",
        tenant_id="test-domain",
        tenant_name="Test Domain",
    )


class TestTenantCredential:
    def test_api_key_credential_fields(self, membership):
        cred = TenantCredential.objects.create(
            tenant_membership=membership,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential="someencryptedvalue",
        )
        assert cred.pk is not None
        assert cred.credential_type == "api_key"

    def test_oauth_credential_fields(self, membership):
        cred = TenantCredential.objects.create(
            tenant_membership=membership,
            credential_type=TenantCredential.OAUTH,
        )
        assert cred.credential_type == "oauth"
        assert cred.encrypted_credential == ""

    def test_one_to_one_with_membership(self, membership):
        TenantCredential.objects.create(
            tenant_membership=membership,
            credential_type=TenantCredential.OAUTH,
        )
        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            TenantCredential.objects.create(
                tenant_membership=membership,
                credential_type=TenantCredential.OAUTH,
            )


class TestResolveCommcareDomains:
    def test_creates_tenant_credential_oauth(self, user, db):
        """resolve_commcare_domains must create TenantCredential(type=oauth) for each membership."""
        from unittest.mock import patch

        from apps.users.services.tenant_resolution import resolve_commcare_domains

        fake_domains = [
            {"domain_name": "domain-a", "project_name": "Domain A"},
            {"domain_name": "domain-b", "project_name": "Domain B"},
        ]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            return_value=fake_domains,
        ):
            memberships = resolve_commcare_domains(user, "fake-token")

        assert len(memberships) == 2
        for tm in memberships:
            cred = TenantCredential.objects.get(tenant_membership=tm)
            assert cred.credential_type == TenantCredential.OAUTH
            assert cred.encrypted_credential == ""

    def test_idempotent_on_re_resolve(self, user, db):
        """Calling resolve twice does not create duplicate TenantCredentials."""
        from unittest.mock import patch

        from apps.users.services.tenant_resolution import resolve_commcare_domains

        fake_domains = [{"domain_name": "domain-a", "project_name": "Domain A"}]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            return_value=fake_domains,
        ):
            resolve_commcare_domains(user, "fake-token")
            resolve_commcare_domains(user, "fake-token")

        assert TenantCredential.objects.filter(tenant_membership__user=user).count() == 1


class TestTenantCredentialEndpoints:
    def test_post_creates_membership_and_credential(self, client, db, user):
        client.force_login(user)
        resp = client.post(
            "/api/auth/tenant-credentials/",
            data={
                "provider": "commcare",
                "tenant_id": "my-domain",
                "tenant_name": "My Domain",
                "credential": "user@example.com:abc123",
            },
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "membership_id" in data

        from apps.users.models import TenantCredential, TenantMembership

        tm = TenantMembership.objects.get(id=data["membership_id"])
        assert tm.provider == "commcare"
        assert tm.tenant_id == "my-domain"
        cred = TenantCredential.objects.get(tenant_membership=tm)
        assert cred.credential_type == TenantCredential.API_KEY

    def test_api_key_stored_encrypted(self, client, db, user):
        """The raw DB value must not contain the plaintext credential."""
        client.force_login(user)
        plaintext = "user@example.com:supersecretkey"
        client.post(
            "/api/auth/tenant-credentials/",
            data={
                "provider": "commcare",
                "tenant_id": "secure-domain",
                "tenant_name": "Secure Domain",
                "credential": plaintext,
            },
            content_type="application/json",
        )
        from apps.users.models import TenantCredential

        cred = TenantCredential.objects.get(tenant_membership__tenant_id="secure-domain")
        assert plaintext not in cred.encrypted_credential
        # Verify round-trip decryption works
        from apps.users.adapters import decrypt_credential

        assert decrypt_credential(cred.encrypted_credential) == plaintext

    def test_get_lists_credentials(self, client, db, user):
        from apps.users.models import TenantCredential, TenantMembership

        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="d1", tenant_name="D1"
        )
        TenantCredential.objects.create(
            tenant_membership=tm, credential_type=TenantCredential.OAUTH
        )
        client.force_login(user)
        resp = client.get("/api/auth/tenant-credentials/")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["credential_type"] == "oauth"
        assert "encrypted_credential" not in items[0]  # never exposed

    def test_delete_removes_credential_and_membership(self, client, db, user):
        from apps.users.models import TenantCredential, TenantMembership

        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="d2", tenant_name="D2"
        )
        TenantCredential.objects.create(
            tenant_membership=tm, credential_type=TenantCredential.OAUTH
        )
        client.force_login(user)
        resp = client.delete(f"/api/auth/tenant-credentials/{tm.id}/")
        assert resp.status_code == 200
        assert not TenantMembership.objects.filter(id=tm.id).exists()

    def test_unauthenticated_returns_401(self, client, db):
        resp = client.post(
            "/api/auth/tenant-credentials/", data={}, content_type="application/json"
        )
        assert resp.status_code == 401
