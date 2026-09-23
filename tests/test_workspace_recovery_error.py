from apps.common.error_codes import ErrorCode
from apps.workspaces.tasks import _ROLE_DENIED_MESSAGE, _workspace_recovery_error


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
