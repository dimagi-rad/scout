from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.api_key_providers import TenantDescriptor

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="dev@example.com", password="pass1234")


@pytest.fixture
def membership(user):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )
    return TenantMembership.objects.create(user=user, tenant=tenant)


class TestTenantConnection:
    def test_api_key_connection_fields(self, user):
        conn = TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.API_KEY,
            encrypted_credential="someencryptedvalue",
        )
        assert conn.pk is not None
        assert conn.credential_type == "api_key"

    def test_oauth_connection_fields(self, user):
        conn = TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.OAUTH,
        )
        assert conn.credential_type == "oauth"
        assert conn.encrypted_credential == ""

    def test_one_oauth_connection_per_user_provider(self, user):
        """At most one OAuth connection per (user, provider) is allowed."""
        TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.OAUTH,
        )
        with pytest.raises(IntegrityError):
            TenantConnection.objects.create(
                user=user,
                provider="commcare",
                credential_type=TenantConnection.OAUTH,
            )

    def test_multiple_api_key_connections_allowed(self, user):
        """The partial uniqueness only applies to OAuth; multiple API-key
        connections per (user, provider) are permitted."""
        TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.API_KEY,
            encrypted_credential="key-1",
        )
        TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.API_KEY,
            encrypted_credential="key-2",
        )
        assert (
            TenantConnection.objects.filter(
                user=user, provider="commcare", credential_type=TenantConnection.API_KEY
            ).count()
            == 2
        )


@pytest.mark.django_db(transaction=True)
class TestResolveCommcareDomains:
    @pytest.mark.asyncio
    async def test_creates_oauth_connection(self, user):
        """resolve_commcare_domains must create a single OAuth TenantConnection
        and link every membership it produces to it."""
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
        # Exactly one OAuth connection for this (user, provider)
        assert (
            await TenantConnection.objects.filter(
                user=user, provider="commcare", credential_type=TenantConnection.OAUTH
            ).acount()
            == 1
        )
        conn = await TenantConnection.objects.aget(user=user, provider="commcare")
        assert conn.encrypted_credential == ""
        # Every membership is linked to that connection
        for tm in memberships:
            refreshed = await TenantMembership.objects.select_related("connection").aget(id=tm.id)
            assert refreshed.connection_id == conn.id

    @pytest.mark.asyncio
    async def test_idempotent_on_re_resolve(self, user):
        """Calling resolve twice does not create duplicate TenantConnections."""
        from apps.users.services.tenant_resolution import resolve_commcare_domains

        fake_domains = [{"domain_name": "domain-a", "project_name": "Domain A"}]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            new_callable=AsyncMock,
            return_value=fake_domains,
        ):
            await resolve_commcare_domains(user, "fake-token")
            await resolve_commcare_domains(user, "fake-token")

        assert await TenantConnection.objects.filter(user=user).acount() == 1


class TestTenantConnectionEndpoints:
    def test_post_creates_membership_and_connection(self, client, db, user):
        client.force_login(user)
        with patch(
            "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
            new_callable=AsyncMock,
            return_value=[TenantDescriptor("my-domain", "my-domain")],
        ):
            resp = client.post(
                "/api/auth/connections/",
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

        tm = TenantMembership.objects.select_related("connection", "tenant").get(id=membership_id)
        assert tm.tenant.provider == "commcare"
        assert tm.tenant.external_id == "my-domain"
        assert tm.connection is not None
        assert tm.connection.credential_type == TenantConnection.API_KEY

    def test_api_key_stored_encrypted(self, client, db, user):
        """The raw DB value must not contain the plaintext credential."""
        from apps.users.adapters import decrypt_credential

        client.force_login(user)
        plaintext = "user@example.com:supersecretkey"
        with patch(
            "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
            new_callable=AsyncMock,
            return_value=[TenantDescriptor("secure-domain", "secure-domain")],
        ):
            client.post(
                "/api/auth/connections/",
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
        tm = TenantMembership.objects.select_related("connection").get(
            tenant__external_id="secure-domain"
        )
        conn = tm.connection
        assert plaintext not in conn.encrypted_credential
        # Verify round-trip decryption works
        assert decrypt_credential(conn.encrypted_credential) == plaintext

    def test_get_lists_connections(self, client, db, user):
        tenant = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
        conn = TenantConnection.objects.create(
            user=user, provider="commcare", credential_type=TenantConnection.OAUTH
        )
        tm.connection = conn
        tm.save(update_fields=["connection"])

        client.force_login(user)
        resp = client.get("/api/auth/connections/")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["credential_type"] == "oauth"
        assert items[0]["connection_id"] == str(conn.id)
        assert "encrypted_credential" not in items[0]  # never exposed
        # The chatbot it credentials is grouped under it
        assert len(items[0]["chatbots"]) == 1
        assert items[0]["chatbots"][0]["tenant_id"] == "d1"

    def test_delete_removes_connection_and_archives_membership(self, client, db, user):
        """DELETE removes the connection and archives its membership (data retained)."""
        tenant = Tenant.objects.create(provider="commcare", external_id="d2", canonical_name="D2")
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
        conn = TenantConnection.objects.create(
            user=user, provider="commcare", credential_type=TenantConnection.OAUTH
        )
        tm.connection = conn
        tm.save(update_fields=["connection"])

        client.force_login(user)
        resp = client.delete(f"/api/auth/connections/{conn.id}/")
        assert resp.status_code == 200
        assert resp.json() == {"status": "removed"}
        assert not TenantConnection.objects.filter(id=conn.id).exists()
        # Membership is archived, not deleted
        tm.refresh_from_db()
        assert tm.archived_at is not None
        assert tm.connection is None

    def test_unauthenticated_returns_401(self, client, db):
        resp = client.post("/api/auth/connections/", data={}, content_type="application/json")
        assert resp.status_code == 401
