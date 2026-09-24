from apps.common.error_codes import ErrorCode
from apps.workspaces.access import WorkspaceAccess
from apps.workspaces.services.access_freshness import VERIFICATION_UNAVAILABLE
from apps.workspaces.tasks import (
    _ROLE_DENIED_MESSAGE,
    _recovery_requester_denied_message,
    _workspace_recovery_error,
)


def test_role_denial_recovery_uses_public_message():
    result = {
        "status": "denied",
        "error_code": ErrorCode.WORKSPACE_ROLE_INSUFFICIENT,
        "error": _ROLE_DENIED_MESSAGE,
        "tenants": [
            {
                "tenant": "t1",
                "success": False,
                "error": _ROLE_DENIED_MESSAGE,
                "error_code": ErrorCode.WORKSPACE_ROLE_INSUFFICIENT,
            }
        ],
    }
    assert _workspace_recovery_error(result, {}) == _ROLE_DENIED_MESSAGE


def test_role_denial_recovery_without_error_text_still_explains():
    result = {"error_code": ErrorCode.WORKSPACE_ROLE_INSUFFICIENT}
    assert _workspace_recovery_error(result, {}) == _ROLE_DENIED_MESSAGE


def test_verification_denial_recovery_is_not_a_source_failure():
    message = "We couldn't verify your access to this workspace right now. Please retry shortly."
    result = {
        "status": "denied",
        "error_code": ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE,
        "error": message,
        "tenants": [
            {
                "tenant": "t1",
                "success": False,
                "error": message,
                "error_code": ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE,
            }
        ],
    }
    assert _workspace_recovery_error(result, {}) == message


def test_mid_run_verification_denial_is_not_a_source_failure():
    message = "We couldn't verify your access to this workspace right now. Please retry shortly."
    result = {
        "tenants": [
            {"tenant": "t1", "success": True},
            {
                "tenant": "t2",
                "success": False,
                "error": message,
                "error_code": ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE,
            },
        ],
    }
    assert _workspace_recovery_error(result, {}) == message


def test_requester_freshness_denial_names_the_access_problem():
    access = WorkspaceAccess(denied_reason=VERIFICATION_UNAVAILABLE)
    message = _recovery_requester_denied_message(access)
    assert "could not be confirmed" in message
    assert "role" not in message
