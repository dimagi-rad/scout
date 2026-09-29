"""Pure rules for deriving workspace and source status (#251).

Callers gather the inputs (schema states, coverage, run states) with whatever
queries suit them — bulk for lists, single-row for detail — and hand them here,
so the rule itself exists once. Nothing in this module touches the database.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from apps.workspaces.models import MaterializationRun, SchemaState

# A PARTIAL run loaded some sources but not all — the data it wrote is present
# and queryable, so it counts as a sync. Excluding it made a workspace whose runs
# are perpetually PARTIAL report last_synced_at=null alongside
# schema_status="available", i.e. "never synced" about data the agent can query.
SYNCED_RUN_STATES = (
    MaterializationRun.RunState.COMPLETED,
    MaterializationRun.RunState.PARTIAL,
)


def derive_schema_status(
    tenant_count: int, active_count: int, provisioning: bool, view_schema_state: str | None
) -> str:
    """The workspace ``schema_status`` the list and detail APIs report.

    Returns "available" | "provisioning" | "unavailable" | "failed".
    ``active_count`` counts sources, not schema rows; callers holding rows should
    go through ``workspace_schema_status``, which does that counting.

    - Single-tenant: available iff every tenant is ACTIVE; provisioning if any is
      mid-provisioning; else unavailable.
    - Multi-tenant: tracked by the view schema — ACTIVE ⇒ available, FAILED ⇒
      failed (per-tenant data may have loaded but there's no queryable surface),
      else provisioning.
    """
    if tenant_count > 1:
        if view_schema_state == SchemaState.ACTIVE:
            return "available"
        if view_schema_state == SchemaState.FAILED:
            return "failed"
        return "provisioning"

    if active_count == tenant_count and tenant_count > 0:
        return "available"
    if provisioning:
        return "provisioning"
    return "unavailable"


def classify_tenant_schemas(rows: Iterable[tuple[Any, str]]) -> tuple[set, set]:
    """``(tenants with an ACTIVE schema, tenants with one mid-provisioning)`` from
    ``(tenant_id, state)`` rows. Counting tenants, not rows, keeps a source with
    two ACTIVE schema rows from reading as a different status in list and detail.
    """
    active, provisioning = set(), set()
    for tenant_id, state in rows:
        if state == SchemaState.ACTIVE:
            active.add(tenant_id)
        elif state == SchemaState.PROVISIONING:
            provisioning.add(tenant_id)
    return active, provisioning


def workspace_schema_status(
    tenant_ids: Iterable[Any], active: set, provisioning: set, view_schema_state: str | None
) -> str:
    """``derive_schema_status`` for a workspace over ``classify_tenant_schemas`` output."""
    tenant_ids = set(tenant_ids)
    return derive_schema_status(
        tenant_count=len(tenant_ids),
        active_count=len(tenant_ids & active),
        provisioning=bool(tenant_ids & provisioning),
        view_schema_state=view_schema_state,
    )


def serving_excluded_tenant_ids(coverage: dict[str, Any] | None) -> set[str]:
    """Tenant ids a serving view explicitly left out, so its data cannot depend on them.

    *coverage* is the output of ``tenant_coverage.parse_coverage``; None (legacy
    or malformed coverage) excludes nothing. A tenant listed as both included and
    excluded counts as included — the view may read it.
    """
    if coverage is None:
        return set()
    return {entry["tenant_id"] for entry in coverage["excluded_tenants"]} - {
        entry["tenant_id"] for entry in coverage["included_tenants"]
    }
