from unittest.mock import MagicMock, patch

import pytest


class TestVerifyCommcareCredential:
    def test_valid_credential_returns_domain_info(self):
        from apps.users.services.tenant_verification import verify_commcare_credential

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "objects": [
                {"domain_name": "dimagi", "project_name": "Dimagi"},
                {"domain_name": "other", "project_name": "Other"},
            ]
        }

        with patch(
            "apps.users.services.tenant_verification.requests.get", return_value=mock_resp
        ) as mock_get:
            result = verify_commcare_credential(
                domain="dimagi", username="user@dimagi.org", api_key="secret"
            )

        assert result["domain_name"] == "dimagi"
        # Verify the global user_domains endpoint is used (no domain in URL path)
        call_args = mock_get.call_args
        assert "/api/user_domains/v1/" in call_args.args[0]

    def test_invalid_credential_raises(self):
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError):
                verify_commcare_credential(
                    domain="dimagi", username="user@dimagi.org", api_key="wrong"
                )

    def test_wrong_domain_raises(self):
        """User is authenticated but doesn't belong to the claimed domain."""
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "objects": [{"domain_name": "some-other-domain", "project_name": "Other"}]
        }

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError, match="not a member of domain"):
                verify_commcare_credential(
                    domain="dimagi", username="user@dimagi.org", api_key="secret"
                )

    def test_no_domains_raises(self):
        """User is authenticated but has no domain memberships."""
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {"objects": []}

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError, match="not a member of domain"):
                verify_commcare_credential(
                    domain="dimagi", username="user@dimagi.org", api_key="secret"
                )
