from unittest.mock import MagicMock, patch

import pytest


class TestVerifyCommcareCredential:
    def test_valid_credential_returns_user_info(self):
        from apps.users.services.tenant_verification import verify_commcare_credential

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "objects": [{"username": "user@dimagi.org", "domain": "dimagi"}]
        }

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp) as mock_get:
            result = verify_commcare_credential(
                domain="dimagi", username="user@dimagi.org", api_key="secret"
            )

        assert result["username"] == "user@dimagi.org"
        # Verify username is passed as query param, not in the URL path
        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["params"]["username"] == "user@dimagi.org"

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
        """User exists but doesn't belong to the claimed domain — empty objects list."""
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {"objects": []}

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError, match="not found in domain"):
                verify_commcare_credential(
                    domain="other-domain", username="user@dimagi.org", api_key="secret"
                )

    def test_404_domain_raises(self):
        """CommCare 404 when the domain itself doesn't exist."""
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError):
                verify_commcare_credential(
                    domain="nonexistent", username="user@dimagi.org", api_key="secret"
                )
