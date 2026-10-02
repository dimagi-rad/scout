"""An unfinished access verification is a retryable 503; a decided denial stays 403."""

import json

import pytest

from apps.common.error_codes import ErrorCode
from apps.users.services.access_verification_types import (
    AccessVerificationResult,
    AccessVerificationStatus,
)
from apps.workspaces.access import (
    VERIFICATION_RETRY_AFTER_SECONDS,
    WorkspaceAccess,
    access_denied_response,
)
from apps.workspaces.services.access_freshness import (
    CREDENTIAL_EXPIRED,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_IN_PROGRESS,
    VERIFICATION_INDETERMINATE,
    VERIFICATION_UNAVAILABLE,
    denial_reason,
)


@pytest.mark.parametrize("reason", [VERIFICATION_UNAVAILABLE, VERIFICATION_IN_PROGRESS])
def test_unfinished_verification_is_503_with_retry_after(reason):
    response = access_denied_response(WorkspaceAccess(denied_reason=reason))

    assert response.status_code == 503
    assert response["Retry-After"] == str(VERIFICATION_RETRY_AFTER_SECONDS)
    body = json.loads(response.content)
    assert body["reason"] == reason
    assert body["retryable"] is True


@pytest.mark.parametrize(
    "reason", [UPSTREAM_ACCESS_LOST, CREDENTIAL_EXPIRED, VERIFICATION_INDETERMINATE]
)
def test_decided_denial_stays_403(reason):
    response = access_denied_response(WorkspaceAccess(denied_reason=reason))

    assert response.status_code == 403
    assert not response.has_header("Retry-After")
    body = json.loads(response.content)
    assert body["reason"] == reason
    assert body["retryable"] is False


def test_extra_fields_never_override_the_denial():
    response = access_denied_response(
        WorkspaceAccess(denied_reason=VERIFICATION_UNAVAILABLE), reason="access_denied"
    )

    assert json.loads(response.content)["reason"] == VERIFICATION_UNAVAILABLE


def test_indeterminate_is_its_own_reason():
    def reason(status, code):
        return denial_reason(AccessVerificationResult(status, code))

    assert (
        reason(AccessVerificationStatus.INDETERMINATE, "verification_indeterminate")
        == VERIFICATION_INDETERMINATE
    )
    assert (
        reason(AccessVerificationStatus.UNAVAILABLE, "verification_unavailable")
        == VERIFICATION_UNAVAILABLE
    )
    assert (
        reason(AccessVerificationStatus.UNAVAILABLE, ErrorCode.AUTH_TOKEN_EXPIRED)
        == CREDENTIAL_EXPIRED
    )
