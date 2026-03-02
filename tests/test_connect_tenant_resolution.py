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
        assert memberships[0].provider == "commcare_connect"
        assert memberships[0].tenant_id == "42"
        assert memberships[0].tenant_name == "Opp 42"
        assert memberships[1].tenant_id == "99"
        assert memberships[1].tenant_name == "Test Opp"

        from apps.users.models import TenantCredential, TenantMembership

        assert TenantMembership.objects.filter(user=user, provider="commcare_connect").count() == 2

        # Verify that an OAUTH TenantCredential was created for each membership
        for tm in TenantMembership.objects.filter(user=user, provider="commcare_connect"):
            assert TenantCredential.objects.filter(
                tenant_membership=tm, credential_type=TenantCredential.OAUTH
            ).exists()

    def test_updates_existing_opportunity_name(self, user):
        from apps.users.models import TenantMembership

        TenantMembership.objects.create(
            user=user,
            provider="commcare_connect",
            tenant_id="42",
            tenant_name="Old Name",
        )

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

        tm = TenantMembership.objects.get(user=user, tenant_id="42", provider="commcare_connect")
        assert tm.tenant_name == "New Name"

    def test_auth_error_raises(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            with pytest.raises(ConnectAuthError):
                resolve_connect_opportunities(user, "fake-token")
