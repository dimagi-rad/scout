# Fix 00#9 — `refresh_tenant_schema` leaves dependent multi-tenant views permanently FAILED

**Issue:** arch-review #236 (Wave 1), finding 00#9 (latent/correctness).
**Scope:** This fixes 00#9 only. The core data-loss bug 00#0 already shipped in PR #271 — this design does **not** touch the refresh→`target_schema` plumbing #271 added.

## Background

A tenant data schema (`t_<id>`) is **shared** across workspaces. Multi-tenant
workspaces query through a `WorkspaceViewSchema` whose namespaced
`{prefix}__{table}` views `SELECT * FROM <tenant_schema>.<table>`, so any change
to a tenant's physical schema must be reflected in every dependent view schema.

Two code paths mutate a tenant's physical schema:

- **`materialize_workspace`** re-materializes a tenant *in place* (same schema
  name), cascade-dropping sibling views. PR #230 handles this by deferring a
  `rebuild_workspace_view_schema` for every sibling multi-tenant workspace
  (`_rebuild_sibling_view_schemas`).
- **`refresh_tenant_schema`** provisions a **new** physical schema
  (`create_refresh_schema` → `{name}_r{uuid}`), materializes into it, swaps it to
  ACTIVE, and schedules teardown of the old schema 30 minutes later.

## Root cause

The refresh path never mirrored #230:

1. **No rebuild after the swap.** After refresh marks the new schema ACTIVE, every
   dependent multi-tenant `WorkspaceViewSchema` still points its views at the
   *old* schema — serving stale data, and (worse) referencing a schema that is
   about to be dropped.
2. **Teardown then falsely fails them.** 30 min later `teardown_schema` runs
   `DROP SCHEMA <old> CASCADE`, cascade-dropping those views, then calls
   `_fail_dependent_view_schemas(schema.tenant_id)` which flips them to FAILED.
   Its comment asserts *"the tenant's data is gone, so a rebuild would rightly
   fail."* **That is false after a refresh:** a new ACTIVE schema exists and a
   rebuild would succeed. The dependent views stay FAILED until something else
   re-materializes the workspace.

## Fix

Mirror the #230 pattern across both halves of the refresh lifecycle.

### 1. `refresh_tenant_schema`: defer dependent rebuilds after the swap

After Step 3 (mark new schema ACTIVE), defer a `rebuild_workspace_view_schema`
for every multi-tenant workspace whose view schema includes this tenant. This is
`_rebuild_sibling_view_schemas` without the "exclude the current workspace"
clause — refresh is per-tenant-membership, not per-workspace, so there is no
workspace to exclude.

### 2. `teardown_schema`: reconcile instead of blindly failing

Replace the unconditional `_fail_dependent_view_schemas(schema.tenant_id)` (and
its false comment) with a reconcile that asks: *does the tenant still have
another ACTIVE schema, excluding the one being torn down?*

- **Yes** (refresh path) → data is intact, views are rebuildable → **defer a
  rebuild** for each dependent workspace; do **not** fail them.
- **No** (pure TTL expiry) → data genuinely gone → **fail** dependent ACTIVE
  views (existing behavior, unchanged).

Deferring a rebuild on the "yes" branch (rather than merely skipping the FAILED
flip) is defense-in-depth: it self-heals even if refresh's earlier rebuild failed
or had not yet run when teardown fired. The rebuild is idempotent and runs in the
background.

**Exclude-by-id is required.** In production both teardown callers
(`expire_inactive_schemas`, `refresh`) flip the schema to TEARDOWN *before*
dispatching `teardown_schema`, so the torn-down schema never counts as ACTIVE.
But the existing unit tests call `teardown_schema` directly on an ACTIVE row.
Excluding the torn-down schema by id makes the "surviving ACTIVE schema?" check
correct in both cases and keeps the existing fail-path tests green.

### 3. Helper refactor (localized)

Generalize the query helper to make workspace exclusion optional and consolidate
the rebuild dispatcher so both paths share it:

- `_sibling_view_schema_workspaces(tenant_ids, exclude_workspace_id)` →
  `_dependent_view_schema_workspaces(tenant_ids, exclude_workspace_id=None)`;
  skip `.exclude()` when `exclude_workspace_id is None`.
- `_rebuild_sibling_view_schemas` → `_rebuild_dependent_view_schemas(tenant_ids,
  *, exclude_workspace_id=None)`. `materialize_workspace` passes
  `exclude_workspace_id=str(workspace.id)`; refresh/teardown pass nothing.

This stays in the helper region (~lines 400–470, 660–690), far from the parallel
#243 effort (resume ~1075, reconciler ~690–816).

## Tests (TDD — written failing first)

In `tests/test_refresh_task.py`:
- **Refresh defers a rebuild** for a dependent multi-tenant workspace sharing the
  refreshed tenant (asserts `rebuild_workspace_view_schema.defer_async` awaited
  with that workspace id). A single-tenant workspace is **not** rebuilt.

In `tests/test_schema_ttl_task.py`:
- **Teardown w/ surviving ACTIVE schema** (refresh path): tearing down the old
  schema while a new ACTIVE schema exists defers a rebuild for the dependent
  workspace and does **not** flip its view schema to FAILED.
- **Regression guard:** the existing pure-expiry tests
  (`test_teardown_schema_fails_dependent_multitenant_view_schemas`,
  `..._does_not_clobber_non_active_view_schema`) still fail dependent ACTIVE
  views when no surviving ACTIVE schema exists — preserved by exclude-by-id.

## Out of scope

- 00#0 / the `target_schema` plumbing from #271.
- `apps/workspaces/tasks.py` resume-prompt and reconciler regions (owned by the
  parallel #243 effort).
