"""User remediation shared by access denials and materialization consumers."""

from collections.abc import Iterable
from typing import TYPE_CHECKING, NamedTuple

from apps.common.error_codes import ErrorCode

if TYPE_CHECKING:
    from apps.workspaces.models import MaterializationRun

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
    ErrorCode.UPSTREAM_UNAVAILABLE: (
        "the provider is temporarily unavailable — nothing is wrong with your "
        "connection; retry shortly."
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


def credential_guidance(failures: Iterable[SourceFailure]) -> list[str]:
    """Return one guidance line per distinct problem, naming what it applies to.

    Ordered by ``CREDENTIAL_GUIDANCE`` rather than by encounter order so the
    wording is stable regardless of which source failed first.
    """
    by_code: dict[str, list[str]] = {}
    for failure in failures:
        if failure.code in CREDENTIAL_GUIDANCE:
            by_code.setdefault(failure.code, []).append(failure.name)
    return [
        f"{', '.join(by_code[code])}: {guidance}"
        for code, guidance in CREDENTIAL_GUIDANCE.items()
        if code in by_code
    ]


def compose_failure_summary(runs: "list[MaterializationRun]") -> str:
    """Compose a human-readable failure summary for ``ThreadJob.error_summary``.

    Reads the per-source state map in ``run.result["sources"]`` (post-#198 shape).
    Returns "" when there is nothing to summarize — callers fall back to a generic
    message.
    """
    if not runs:
        return ""

    failed_sources: list[SourceFailure] = []
    completed_sources: list[tuple[str, int]] = []
    skipped_sources: list[str] = []
    cancelled_sources: list[str] = []

    for run in runs:
        result = run.result if isinstance(run.result, dict) else None
        if not result:
            continue
        top_level_error = result.get("error")
        if top_level_error:
            failed_sources.append(
                SourceFailure(
                    name="materialization",
                    error=str(top_level_error),
                    code=str(result.get("error_code") or ErrorCode.INTERNAL_ERROR),
                )
            )
        for name, info in (result.get("sources") or {}).items():
            if not isinstance(info, dict):
                continue
            state = info.get("state")
            if state == "failed":
                failed_sources.append(
                    SourceFailure(
                        name=name,
                        error=str(info.get("error") or "unknown error"),
                        code=str(info.get("error_code") or ErrorCode.INTERNAL_ERROR),
                    )
                )
            elif state == "completed":
                completed_sources.append((name, int(info.get("rows") or 0)))
            elif state == "skipped":
                skipped_sources.append(name)
            elif state == "cancelled":
                cancelled_sources.append(name)

    parts: list[str] = []
    if failed_sources:
        # Every failure carries its own message. Rendering only the first and
        # listing the rest as bare names discarded the very string the loaders
        # are asked to produce, and left a second failure indistinguishable
        # from a skipped source (#388 review).
        parts.append("; ".join(f"{f.name} failed: {f.error.rstrip('.')}" for f in failed_sources))
    if completed_sources:
        total_rows = sum(rows for _, rows in completed_sources)
        names = ", ".join(n for n, _ in completed_sources)
        parts.append(f"{names} ({total_rows:,} rows) loaded successfully")
    if skipped_sources:
        parts.append(f"remaining sources skipped: {', '.join(skipped_sources)}")
    if cancelled_sources:
        parts.append(f"cancelled: {', '.join(cancelled_sources)}")

    if not parts:
        # No per-source detail (failure before any source ran) — surface run state.
        states = sorted({r.state for r in runs})
        return f"Materialization {'/'.join(states)}."
    summary = ". ".join(parts) + "."
    for line in credential_guidance(failed_sources):
        summary += " " + line
    return summary
