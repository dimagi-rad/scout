from apps.workspaces.access import tool_write_denied
from apps.workspaces.tasks import _workspace_recovery_error


def test_role_denial_recovery_uses_public_message():
    result = tool_write_denied()
    assert _workspace_recovery_error(result, {}) == result["error"]["message"]
