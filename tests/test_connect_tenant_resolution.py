from unittest.mock import MagicMock, patch

import pytest

from apps.users.services.tenant_resolution import ConnectAuthError, resolve_connect_opportunities


@pytest.mark.django_db
class TestResolveConnectOpportunities:
    def test_fetches_and_stores_opportunities(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "opportunities": [
                {"id": 42, "name": "Opp 42"},
                {"id": 99, "name": "Test Opp"},
            ],
        }

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            memberships = resolve_connect_opportunities(user, "fake-token")

        assert len(memberships) == 2
        assert memberships[0].tenant.provider == "commcare_connect"
        assert memberships[0].tenant.external_id == "42"
        assert memberships[0].tenant.canonical_name == "Opp 42"
        assert memberships[1].tenant.external_id == "99"
        assert memberships[1].tenant.canonical_name == "Test Opp"

        from apps.users.models import TenantCredential, TenantMembership

        assert (
            TenantMembership.objects.filter(user=user, tenant__provider="commcare_connect").count()
            == 2
        )

        # Verify that an OAUTH TenantCredential was created for each membership
        for tm in TenantMembership.objects.filter(user=user, tenant__provider="commcare_connect"):
            assert TenantCredential.objects.filter(
                tenant_membership=tm, credential_type=TenantCredential.OAUTH
            ).exists()

    def test_updates_existing_opportunity_name(self, user):
        from apps.users.models import Tenant, TenantMembership

        tenant = Tenant.objects.create(
            provider="commcare_connect", external_id="42", canonical_name="Old Name"
        )
        TenantMembership.objects.create(user=user, tenant=tenant)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "opportunities": [
                {"id": 42, "name": "New Name"},
            ],
        }

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            resolve_connect_opportunities(user, "fake-token")

        tenant.refresh_from_db()
        assert tenant.canonical_name == "New Name"

    def test_auth_error_raises(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            with pytest.raises(ConnectAuthError):
                resolve_connect_opportunities(user, "fake-token")
