"""User remediation shared by access denials and materialization consumers."""

from apps.common.error_codes import ErrorCode

# Loaders describe failures; only presentation consumers add this advice.
CREDENTIAL_GUIDANCE: dict[str, str] = {
    ErrorCode.AUTH_CREDENTIAL_MISSING: (
        "no usable sign-in is available — open Connected Accounts and connect or "
        "reconnect the affected account before retrying."
    ),
    ErrorCode.PIPELINE_UNRESOLVED: (
        "ask an administrator to configure or repair the materialization pipeline "
        "for this provider before retrying. Re-running cannot resolve this pipeline "
        "configuration problem until that configuration changes."
    ),
    ErrorCode.AUTH_TOKEN_EXPIRED: (
        "expired or revoked sign-in — reconnect the affected account "
        "(Settings → Connections) and re-run materialization."
    ),
    ErrorCode.AUTH_REFRESH_FAILED: (
        "sign-in refresh could not complete — retry shortly. If the problem persists, "
        "ask an administrator to check the provider connection settings."
    ),
    ErrorCode.AUTH_ACCESS_DENIED: (
        "access was removed upstream or this resource is restricted — reconnecting "
        "alone does not change upstream permissions. "
        "Ask an admin on the affected provider to restore access, or remove that "
        "data source from the workspace."
    ),
    ErrorCode.WORKSPACE_TENANT_UNREACHABLE: (
        "in this workspace but not connected to your account, so this run did not "
        "refresh it — connect that account "
        "(Settings → Connections) if you should have access, or ask a workspace "
        "admin to move it to its own workspace."
    ),
}

# A plain Retry cannot repair these prerequisites. Transient refresh failures
# remain retryable; a 401 and an authoritative 403 must not be treated alike.
REQUIRES_REMEDIATION = frozenset(
    {
        ErrorCode.AUTH_CREDENTIAL_MISSING,
        ErrorCode.AUTH_TOKEN_EXPIRED,
        ErrorCode.AUTH_ACCESS_DENIED,
        ErrorCode.WORKSPACE_TENANT_UNREACHABLE,
        ErrorCode.PIPELINE_UNRESOLVED,
    }
)
