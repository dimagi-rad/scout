from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import TenantCredential, TenantMembership

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="dev@example.com", password="pass1234")


@pytest.fixture
def membership(user):
    from apps.users.models import Tenant

    tenant = Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )
    return TenantMembership.objects.create(user=user, tenant=tenant)


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


@pytest.mark.django_db(transaction=True)
class TestResolveCommcareDomains:
    @pytest.mark.asyncio
    async def test_creates_tenant_credential_oauth(self, user):
        """resolve_commcare_domains must create TenantCredential(type=oauth) for each membership."""
        from apps.users.services.tenant_resolution import resolve_commcare_domains

        fake_domains = [
            {"domain_name": "domain-a", "project_name": "Domain A"},
            {"domain_name": "domain-b", "project_name": "Domain B"},
        ]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            new_callable=AsyncMock,
            return_value=fake_domains,
        ):
            memberships = await resolve_commcare_domains(user, "fake-token")

        assert len(memberships) == 2
        for tm in memberships:
            cred = await TenantCredential.objects.aget(tenant_membership=tm)
            assert cred.credential_type == TenantCredential.OAUTH
            assert cred.encrypted_credential == ""

    @pytest.mark.asyncio
    async def test_idempotent_on_re_resolve(self, user):
        """Calling resolve twice does not create duplicate TenantCredentials."""
        from apps.users.services.tenant_resolution import resolve_commcare_domains

        fake_domains = [{"domain_name": "domain-a", "project_name": "Domain A"}]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            new_callable=AsyncMock,
            return_value=fake_domains,
        ):
            await resolve_commcare_domains(user, "fake-token")
            await resolve_commcare_domains(user, "fake-token")

        assert await TenantCredential.objects.filter(tenant_membership__user=user).acount() == 1


class TestTenantCredentialEndpoints:
    def test_post_creates_membership_and_credential(self, client, db, user):
        from apps.users.services.api_key_providers import TenantDescriptor

        client.force_login(user)
        with patch(
            "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
            new_callable=AsyncMock,
            return_value=[TenantDescriptor("my-domain", "my-domain")],
        ):
            resp = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "fields": {
                        "domain": "my-domain",
                        "username": "user@example.com",
                        "api_key": "abc123",
                    },
                },
                content_type="application/json",
            )
        assert resp.status_code == 201
        body = resp.json()
        assert "memberships" in body
        membership = body["memberships"][0]
        membership_id = membership["membership_id"]

        from apps.users.models import TenantCredential, TenantMembership

        tm = TenantMembership.objects.get(id=membership_id)
        assert tm.tenant.provider == "commcare"
        assert tm.tenant.external_id == "my-domain"
        cred = TenantCredential.objects.get(tenant_membership=tm)
        assert cred.credential_type == TenantCredential.API_KEY

    def test_api_key_stored_encrypted(self, client, db, user):
        """The raw DB value must not contain the plaintext credential."""
        from apps.users.services.api_key_providers import TenantDescriptor

        client.force_login(user)
        plaintext = "user@example.com:supersecretkey"
        with patch(
            "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
            new_callable=AsyncMock,
            return_value=[TenantDescriptor("secure-domain", "secure-domain")],
        ):
            client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "fields": {
                        "domain": "secure-domain",
                        "username": "user@example.com",
                        "api_key": "supersecretkey",
                    },
                },
                content_type="application/json",
            )
        from apps.users.models import TenantCredential

        cred = TenantCredential.objects.get(tenant_membership__tenant__external_id="secure-domain")
        assert plaintext not in cred.encrypted_credential
        # Verify round-trip decryption works
        from apps.users.adapters import decrypt_credential

        assert decrypt_credential(cred.encrypted_credential) == plaintext

    def test_get_lists_credentials(self, client, db, user):
        from apps.users.models import Tenant, TenantCredential, TenantMembership

        tenant = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
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
        from apps.users.models import Tenant, TenantCredential, TenantMembership

        tenant = Tenant.objects.create(provider="commcare", external_id="d2", canonical_name="D2")
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
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
