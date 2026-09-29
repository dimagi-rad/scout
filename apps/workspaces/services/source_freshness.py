"""Per-source data freshness: when each source was last fetched, and what the
workspace's latest load did with it (#715).

A load can finish "completed" while a source kept serving an old snapshot: its
credential failed preflight, or an equivalent published load was reused without
fetching. The run summary, ``get_schema_status`` and the agent prompt read this
module so they say which sources were not refreshed and what fixes each one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.common.error_codes import ErrorCode
from apps.users.models import PROVIDER_CHOICES
from apps.workspaces.models import SchemaState, WorkspaceTenant
from apps.workspaces.services.failure_guidance import CREDENTIAL_GUIDANCE
from apps.workspaces.services.query_state import synced_runs

logger = logging.getLogger(__name__)

REFRESHED = "refreshed"
REUSED = "reused"
SKIPPED = "skipped"

_PROVIDER_LABELS = dict(PROVIDER_CHOICES)

# A new sign-in fixes these. A 403 (AUTH_ACCESS_DENIED) does not: reconnecting mints
# an identically scoped credential that is refused the same way (#372).
_RECONNECT_CODES = frozenset(
    {
        ErrorCode.AUTH_TOKEN_EXPIRED,
        ErrorCode.AUTH_CREDENTIAL_MISSING,
        ErrorCode.WORKSPACE_TENANT_UNREACHABLE,
    }
)


def provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider or "the provider")


def load_outcome(entry: dict) -> dict:
    """Classify one tenant entry of a workspace load summary."""
    result = entry.get("result") or {}
    if entry.get("success"):
        reused = result.get("reused") or result.get("status") == "already_loaded"
        return {"refresh": REUSED if reused else REFRESHED}
    outcome = {"refresh": SKIPPED, "error_code": str(entry.get("error_code") or "")}
    if entry.get("cancelled"):
        outcome["cancelled"] = True
    return outcome


def remedy(error_code: str, provider: str) -> str:
    """What gets a source that was not refreshed fetched again."""
    label = provider_label(provider)
    if error_code in _RECONNECT_CODES:
        return f"reconnect {label} in Connected Accounts, then refresh the data"
    if error_code == ErrorCode.AUTH_ACCESS_DENIED:
        return (
            f"{label} refused access to this source, so reconnecting alone will not "
            f"restore it: a {label} admin must restore access, or the source can be "
            "removed from the workspace"
        )
    if error_code == ErrorCode.WORKSPACE_TENANT_SKIPPED:
        return "fix the other sources listed as not refreshed, then refresh the data"
    if error_code in CREDENTIAL_GUIDANCE:
        return CREDENTIAL_GUIDANCE[error_code]
    return "refresh the data again; if it fails the same way, report the error"


async def _last_fetched(tenant_ids: Iterable) -> dict[str, datetime]:
    """When each tenant's serving snapshot was fetched: its newest synced run."""
    fetched: dict[str, datetime] = {}
    rows = synced_runs().filter(
        tenant_schema__tenant_id__in=list(tenant_ids),
        tenant_schema__state=SchemaState.ACTIVE,
    )
    async for tenant_id, completed_at in rows.values_list(
        "tenant_schema__tenant_id", "completed_at"
    ):
        fetched.setdefault(str(tenant_id), completed_at)
    return fetched


async def arecord_load_outcomes(workspace_id, tenant_results: list[dict]) -> list[dict]:
    """Persist what this load did with each source and return it for the run result.

    Never raises: the load already happened, and its summary must still return.
    """
    try:
        now = timezone.now()
        entries = [entry for entry in tenant_results if entry.get("tenant_id")]
        fetched = await _last_fetched(entry["tenant_id"] for entry in entries)
        sources = []
        for entry in entries:
            outcome = {**load_outcome(entry), "at": now.isoformat()}
            await WorkspaceTenant.objects.filter(
                workspace_id=workspace_id, tenant_id=entry["tenant_id"]
            ).aupdate(last_load=outcome)
            last = fetched.get(entry["tenant_id"])
            source = {
                "tenant_id": entry["tenant_id"],
                "tenant": entry.get("display_name") or entry.get("tenant"),
                "provider": entry.get("provider", ""),
                "last_fetched_at": last.isoformat() if last else None,
                **outcome,
            }
            if outcome["refresh"] == SKIPPED:
                source["remedy"] = remedy(outcome["error_code"], source["provider"])
            sources.append(source)
        return sources
    except Exception:
        logger.exception("Could not record per-source load outcomes for %s", workspace_id)
        return []


async def aworkspace_source_freshness(workspace_id) -> list[dict]:
    """Every source of the workspace with its data age and latest load outcome.

    ``not_refreshed`` is True when the workspace's latest load skipped the source
    and nothing has fetched it since (another workspace sharing the source may
    have); ``remedy`` then says what fixes it.
    """
    rows = [
        wt
        async for wt in WorkspaceTenant.objects.filter(workspace_id=workspace_id)
        .select_related("tenant")
        .order_by("tenant__canonical_name", "tenant__external_id")
    ]
    fetched = await _last_fetched(wt.tenant_id for wt in rows)
    sources = []
    for wt in rows:
        tenant = wt.tenant
        last = fetched.get(str(tenant.id))
        outcome = wt.last_load if isinstance(wt.last_load, dict) else {}
        attempted_at = parse_datetime(str(outcome.get("at") or ""))
        not_refreshed = outcome.get("refresh") == SKIPPED and not (
            last and attempted_at and last > attempted_at
        )
        source = {
            "tenant_id": str(tenant.id),
            "tenant": tenant.external_id,
            "name": tenant.canonical_name or tenant.external_id,
            "provider": tenant.provider,
            "last_fetched_at": last.isoformat() if last else None,
            "last_load": outcome.get("refresh"),
            "not_refreshed": not_refreshed,
        }
        if not_refreshed:
            source["error_code"] = outcome.get("error_code") or ""
            source["remedy"] = remedy(source["error_code"], tenant.provider)
        sources.append(source)
    return sources
