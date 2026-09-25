"""User remediation shared by access denials and materialization consumers."""

from collections.abc import Iterable
from typing import NamedTuple

from apps.common.error_codes import ErrorCode

# Loaders describe failures; only presentation consumers add this advice.
# These are fragments: each consumer prefixes them with the affected source names.
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
        "not connected to your account — connect that account (Settings → Connections) "
        "if you disconnected it or have not connected it yet. If access was removed "
        "or restricted at the provider, ask a provider admin to restore it; "
        "reconnecting alone cannot restore those permissions. A workspace admin "
        "can help remove a source you no longer need."
    ),
    ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE: (
        "access could not be confirmed with the provider just now — nothing was "
        "removed; retry shortly."
    ),
}

# Stored credential failures must not permanently hide Retry after reconnecting.
# Only an authoritative denial or pipeline configuration defect blocks it here.
BLOCKS_IMMEDIATE_RETRY = frozenset(
    {
        ErrorCode.AUTH_ACCESS_DENIED,
        ErrorCode.PIPELINE_UNRESOLVED,
    }
)


class SourceFailure(NamedTuple):
    """A failure attributed to a source, or the tenant when preflight never ran sources."""

    name: str
    error: str
    code: str


def summary_failures(tenant_summaries: Iterable[dict]) -> list[SourceFailure]:
    """Include preflight refusals: no MaterializationRun exists until run_pipeline starts."""
    failures: list[SourceFailure] = []
    for tenant in tenant_summaries:
        if not isinstance(tenant, dict):
            continue
        if tenant.get("error_code") or tenant.get("error"):
            failures.append(
                SourceFailure(
                    name=str(tenant.get("display_name") or tenant.get("tenant") or "unknown"),
                    error=str(tenant.get("error") or ""),
                    code=str(tenant.get("error_code") or ErrorCode.INTERNAL_ERROR),
                )
            )
        for name, src in (tenant.get("sources") or {}).items():
            if not isinstance(src, dict):
                continue
            if src.get("state") == "completed" or not (
                src.get("error")
                or src.get("error_code")
                or src.get("state") in {"failed", "cancelled"}
            ):
                continue
            failures.append(
                SourceFailure(
                    name=name,
                    error=str(src.get("error") or ""),
                    code=str(src.get("error_code") or ErrorCode.INTERNAL_ERROR),
                )
            )
    return failures
