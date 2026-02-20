from unittest.mock import MagicMock, patch

import pytest

from apps.users.services.tenant_resolution import resolve_commcare_domains


@pytest.mark.django_db
class TestResolveCommcareDomains:
    def test_fetches_and_stores_domains(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "meta": {"limit": 20, "offset": 0, "total_count": 2, "next": None},
            "objects": [
                {"domain_name": "dimagi", "project_name": "Dimagi"},
                {"domain_name": "test-project", "project_name": "Test Project"},
            ],
        }

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            memberships = resolve_commcare_domains(user, "fake-token")

        assert len(memberships) == 2
        assert memberships[0].tenant_id == "dimagi"
        assert memberships[1].tenant_id == "test-project"

        from apps.users.models import TenantMembership

        assert TenantMembership.objects.filter(user=user).count() == 2

    def test_updates_existing_memberships(self, user):
        from apps.users.models import TenantMembership

        TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Old Name"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "meta": {"limit": 20, "offset": 0, "total_count": 1, "next": None},
            "objects": [{"domain_name": "dimagi", "project_name": "New Name"}],
        }

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            resolve_commcare_domains(user, "fake-token")

        tm = TenantMembership.objects.get(user=user, tenant_id="dimagi")
        assert tm.tenant_name == "New Name"

    def test_api_error_raises(self, user):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status.side_effect = Exception("Unauthorized")

        with patch(
            "apps.users.services.tenant_resolution.requests.get",
            return_value=mock_response,
        ):
            with pytest.raises(Exception):  # noqa: B017
                resolve_commcare_domains(user, "fake-token")
