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
    tenant_count: int, active_count: int, load_running: bool, view_schema_state: str | None
) -> str:
    """The workspace ``schema_status`` every surface reports.

    Returns "available" | "provisioning" | "failed" | "not_loaded".
    ``active_count`` counts sources, not schema rows; callers holding rows should
    go through ``workspace_schema_status``, which does that counting.

    - available: data is serving. A single source serves from its ACTIVE schema,
      several sources from their ACTIVE view schema.
    - provisioning: nothing serves yet and a load or build is actually running.
      A PROVISIONING row alone is not one: a load that died leaves it behind (#249).
    - failed: the multi-source view schema failed to build and nothing is retrying.
    - not_loaded: nothing serves and nothing is loading.
    """
    if tenant_count > 1:
        serving = view_schema_state == SchemaState.ACTIVE
    else:
        serving = tenant_count > 0 and active_count == tenant_count
    if serving:
        return "available"
    if load_running:
        return "provisioning"
    if tenant_count > 1 and view_schema_state == SchemaState.FAILED:
        return "failed"
    return "not_loaded"


def workspace_schema_status(
    tenant_ids: Iterable[Any], active: set, load_running: bool, view_schema_state: str | None
) -> str:
    """``derive_schema_status`` over the ids of tenants holding an ACTIVE schema.

    Counting tenants, not schema rows, keeps a source with two ACTIVE schema rows
    from reading as a different status in list and detail.
    """
    tenant_ids = set(tenant_ids)
    return derive_schema_status(
        tenant_count=len(tenant_ids),
        active_count=len(tenant_ids & active),
        load_running=load_running,
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
