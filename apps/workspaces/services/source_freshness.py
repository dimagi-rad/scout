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

from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.common.error_codes import ErrorCode
from apps.users.models import PROVIDER_CHOICES
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant, WorkspaceViewSchema
from apps.workspaces.services.query_state import synced_runs
from apps.workspaces.services.status import serving_excluded_tenant_ids
from apps.workspaces.services.tenant_coverage import parse_coverage

logger = logging.getLogger(__name__)

REFRESHED = "refreshed"
REUSED = "reused"
SKIPPED = "skipped"

_PROVIDER_LABELS = dict(PROVIDER_CHOICES)

# A new sign-in by the member who ran the load fixes these; any member whose own
# sign-in works can also refresh. Everything else is not about a credential.
CREDENTIAL_CODES = frozenset({ErrorCode.AUTH_TOKEN_EXPIRED, ErrorCode.AUTH_CREDENTIAL_MISSING})


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


def remedy(outcome: dict, provider: str, *, own_load: bool = True) -> str:
    """What gets a source that was not refreshed fetched again.

    ``own_load`` says whether the viewer ran that load: a sign-in failure is the
    loading member's, and another member whose sign-in works can simply refresh.
    """
    label = provider_label(provider)
    code = outcome.get("error_code") or ""
    if outcome.get("cancelled") or outcome.get("not_reached"):
        return "the load stopped before it fetched this source; refresh the data again"
    if code in CREDENTIAL_CODES:
        if own_load:
            return f"reconnect {label} in Connected Accounts, then refresh the data"
        return (
            f"the member who ran the last load has to reconnect {label} in Connected "
            f"Accounts, or a member whose {label} sign-in works can refresh the data"
        )
    if code == ErrorCode.WORKSPACE_TENANT_UNREACHABLE:
        return (
            f"connect or reconnect {label} in Connected Accounts if that account was "
            f"disconnected; if access was removed at {label}, reconnecting cannot restore "
            f"it and a {label} admin must restore access"
        )
    if code == ErrorCode.AUTH_ACCESS_DENIED:
        return (
            f"{label} refused access to this source, so reconnecting will not restore it: "
            f"a {label} admin must restore access, or the source can be removed from the "
            "workspace"
        )
    if code == ErrorCode.WORKSPACE_TENANT_SKIPPED:
        return "fix the other sources listed as not refreshed, then refresh the data"
    if code in {ErrorCode.AUTH_REFRESH_FAILED, ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE}:
        return "the sign-in could not be checked just now; refresh the data again shortly"
    if code == ErrorCode.PIPELINE_UNRESOLVED:
        return f"an administrator must configure the {label} loader; a refresh cannot fix it"
    return "refresh the data again; if it fails the same way, report the error to an administrator"


async def _last_fetched(tenant_ids: Iterable) -> dict[str, datetime]:
    """When each tenant's serving snapshot was fetched: its newest synced run."""
    rows = (
        synced_runs()
        .filter(
            tenant_schema__tenant_id__in=list(tenant_ids),
            tenant_schema__state=SchemaState.ACTIVE,
        )
        .order_by()
        .values("tenant_schema__tenant_id")
        .annotate(fetched=Max("completed_at"))
    )
    return {str(row["tenant_schema__tenant_id"]): row["fetched"] async for row in rows}


async def arecord_load_outcomes(workspace_id, tenant_results: list[dict], user_id="") -> list:
    """Persist what this load did with each source and return it for the run result.

    A source that was only published as already loaded keeps its earlier record:
    nothing checked its credential, so "reused" would clear a standing skip. A
    source the load never reached (it was cancelled first) is recorded as skipped.
    Never raises: the load already happened, and its summary must still return.
    """
    try:
        now = timezone.now().isoformat()
        entries = {e["tenant_id"]: e for e in tenant_results if e.get("tenant_id")}
        workspace_tenants = [
            wt
            async for wt in WorkspaceTenant.objects.filter(workspace_id=workspace_id)
            .select_related("tenant")
            .order_by("tenant__canonical_name", "tenant__external_id")
        ]
        fetched = await _last_fetched(wt.tenant_id for wt in workspace_tenants)
        sources = []
        for wt in workspace_tenants:
            tenant_id = str(wt.tenant_id)
            entry = entries.get(tenant_id)
            if entry is None:
                outcome = {"refresh": SKIPPED, "error_code": "", "not_reached": True}
            else:
                outcome = load_outcome(entry)
            outcome.update(at=now, by=str(user_id or ""))
            only_published = entry is not None and (
                (entry.get("result") or {}).get("status") == "already_loaded"
            )
            if not only_published:
                await WorkspaceTenant.objects.filter(id=wt.id).aupdate(last_load=outcome)
            last = fetched.get(tenant_id)
            source = {
                "tenant_id": tenant_id,
                "tenant": (entry or {}).get("display_name") or wt.tenant.external_id,
                "provider": wt.tenant.provider,
                "last_fetched_at": last.isoformat() if last else None,
                **{k: v for k, v in outcome.items() if k not in {"at", "by"}},
            }
            if outcome["refresh"] == SKIPPED:
                source["remedy"] = remedy(outcome, wt.tenant.provider)
            sources.append(source)
        return sources
    except Exception:
        logger.exception("Could not record per-source load outcomes for %s", workspace_id)
        return []


async def _excluded_from_view(workspace_id, tenant_count: int) -> set[str]:
    if tenant_count < 2:
        return set()
    coverage = await (
        WorkspaceViewSchema.objects.filter(workspace_id=workspace_id, state=SchemaState.ACTIVE)
        .values_list("tenant_coverage", flat=True)
        .afirst()
    )
    return serving_excluded_tenant_ids(parse_coverage(coverage))


async def aworkspace_source_freshness(workspace_id, viewer_id="") -> list[dict]:
    """Every source of the workspace with its data age and latest load outcome.

    ``not_refreshed`` is True when the workspace's latest load skipped the source
    and nothing has fetched it since (another workspace sharing the source may
    have); ``remedy`` then says what fixes it. ``serving`` says whether the
    source's data is in what the workspace queries. Never raises: callers are a
    prompt build and a status tool, which must still answer without it.
    """
    try:
        return await _source_freshness(workspace_id, str(viewer_id or ""))
    except Exception:
        logger.exception("Could not read per-source freshness for %s", workspace_id)
        return []


async def _source_freshness(workspace_id, viewer_id: str) -> list[dict]:
    rows = [
        wt
        async for wt in WorkspaceTenant.objects.filter(workspace_id=workspace_id)
        .select_related("tenant")
        .order_by("tenant__canonical_name", "tenant__external_id")
    ]
    tenant_ids = [wt.tenant_id for wt in rows]
    fetched = await _last_fetched(tenant_ids)
    active = {
        str(tenant_id)
        async for tenant_id in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids, state=SchemaState.ACTIVE
        ).values_list("tenant_id", flat=True)
    }
    excluded = await _excluded_from_view(workspace_id, len(rows))
    sources = []
    for wt in rows:
        tenant = wt.tenant
        tenant_id = str(tenant.id)
        last = fetched.get(tenant_id)
        outcome = wt.last_load if isinstance(wt.last_load, dict) else {}
        attempted_at = parse_datetime(str(outcome.get("at") or ""))
        not_refreshed = outcome.get("refresh") == SKIPPED and not (
            last and attempted_at and last > attempted_at
        )
        source = {
            "tenant_id": tenant_id,
            "tenant": tenant.external_id,
            "name": tenant.canonical_name or tenant.external_id,
            "provider": tenant.provider,
            "serving": tenant_id in active and tenant_id not in excluded,
            "last_fetched_at": last.isoformat() if last else None,
            "last_load": outcome.get("refresh"),
            "not_refreshed": not_refreshed,
        }
        if not_refreshed:
            loader = outcome.get("by") or ""
            source["error_code"] = outcome.get("error_code") or ""
            if outcome.get("cancelled") or outcome.get("not_reached"):
                source["stopped"] = True
            source["remedy"] = remedy(
                outcome, tenant.provider, own_load=not loader or loader == viewer_id
            )
        sources.append(source)
    return sources
