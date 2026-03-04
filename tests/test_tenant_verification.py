from unittest.mock import MagicMock, patch

import pytest


class TestVerifyCommcareCredential:
    def test_valid_credential_returns_domain_info(self):
        from apps.users.services.tenant_verification import verify_commcare_credential

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"username": "user@dimagi.org", "domain": "dimagi"}

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            result = verify_commcare_credential(
                domain="dimagi", username="user@dimagi.org", api_key="secret"
            )

        assert result["domain"] == "dimagi"

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
        """User exists but doesn't belong to the claimed domain."""
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError):
                verify_commcare_credential(
                    domain="other-domain", username="user@dimagi.org", api_key="secret"
                )
