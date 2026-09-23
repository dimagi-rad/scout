from apps.common.error_codes import ErrorCode
from apps.workspaces.access import TOOL_WRITE_DENIED_MESSAGE
from apps.workspaces.tasks import _workspace_recovery_error


def test_role_denial_recovery_uses_public_message():
    result = {
        "status": "denied",
        "error_code": ErrorCode.WORKSPACE_ROLE_INSUFFICIENT,
        "error": TOOL_WRITE_DENIED_MESSAGE,
        "tenants": [
            {
                "tenant": "t1",
                "success": False,
                "error": TOOL_WRITE_DENIED_MESSAGE,
                "error_code": ErrorCode.WORKSPACE_ROLE_INSUFFICIENT,
            }
        ],
    }
    assert _workspace_recovery_error(result, {}) == TOOL_WRITE_DENIED_MESSAGE
