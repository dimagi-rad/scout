# Tenant-access refresh + live enforcement (Root Cause A)

**Date:** 2026-06-18
**Status:** Design — pending review
**Branch:** `bdr/tenant-access-refresh`

## Problem

Scout learns "user X belongs to opp/domain/team Y" **only** when X personally completes an
OAuth login. `resolve_commcare_domains` / `resolve_connect_opportunities` /
`resolve_ocs_chatbots` (`apps/users/services/tenant_resolution.py`) run from
`resolve_tenant_on_social_login` (`apps/users/signals.py`) at login, and from
`tenant_list_view` (`apps/users/views.py`) for the **currently logged-in** user on a TTL cache.

Two consequences:

1. **Grant staleness.** When X is added to an opp in Connect *after* their last Scout login, Scout
   never finds out until X logs in again. A manager (B) trying to share a workspace with X hits the
   `TenantMembership` check (`apps/workspaces/api/workspace_views.py`) against X's frozen data and
   fails with "User is not part of this workspace's tenants." Scout never refreshes **X** (the
   target) — only the sharer refreshes themselves. This is the original nlesh failure.

2. **Revocation gap.** The resolver is **additive-only** (`aget_or_create` + un-archive; never
   archives lost access). When upstream removes a user from an opp/domain/team, Scout keeps serving
   that access indefinitely.

Underlying both: access is gated **inconsistently** across the codebase.

| Gate | Checks | Honors tenant access? |
|---|---|---|
| `resolve_workspace` / `_drf` / `aresolve_workspace` | `WorkspaceMembership` only | ❌ never |
| chat `_resolve_workspace_and_membership` — multi-tenant | `WorkspaceMembership` only | ❌ tenant check skipped |
| chat — single-tenant | `TenantMembership.aget` | ⚠️ no `archived_at` filter |
| MCP `_resolve_workspace_memberships` | `TenantMembership` by tenant+user | ⚠️ no `archived_at` filter |

So even archiving a membership would not reliably revoke access, and a multi-tenant workspace member
can currently query data with **no** tenant membership at all.

## Goals

- **Grant:** reflect newly-granted upstream access promptly — especially at share time — without the
  target manually disconnecting/reconnecting.
- **Revocation:** when upstream removes access, stop serving it to **every** Scout user and manager,
  with **no human-in-Scout path to reinstate it**.
- **Robustness:** make the access rule structurally un-missable — one authorizer, safe-by-default
  data access, and a CI check that fails if anything bypasses it.

## Non-goals (explicit)

- **C — invite / pre-share for non-Scout users** ("No Scout user with that email"). Separate.
- **Periodic background sweep.** Activity-triggered refresh bounds revocation staleness for now;
  a Procrastinate cron is a noted follow-up.
- **Content soft-delete / recovery (Case 1, below).** Out of scope and explicitly fenced off.

## Two distinct meanings of "archived" — never share a code path

| | What it is | Who can revive it |
|---|---|---|
| **Case 1 — content soft-delete** | a user deletes their *own* work (workspace, artifact, recipe) | recoverable by a person in Scout (undo) |
| **Case 2 — access revoked upstream** | Connect/HQ/OCS removed the user from a tenant | **no one in Scout** — only (a) upstream re-grant → Scout re-syncs, or (b) a superuser at the DB |

This design touches **only Case 2** (the `TenantMembership` access layer). An archived
`TenantMembership` is a **tombstone, not an undo**: there is deliberately **no** user/manager/admin
affordance to reinstate it. The only un-archive paths are the resolver reflecting an upstream
re-grant, and direct DB surgery. The spec forbids any Case-1 recovery code from ever flipping a
`TenantMembership`'s archived state.

## Locked decisions

- **Scope:** grant + revocation (full sync).
- **Revocation model:** live enforcement — compute effective access; non-destructive to
  `WorkspaceMembership` rows (the row means "was invited"; effective access is recomputed).
- **Manager gating:** unified — no live tenant ⇒ no workspace (manage *or* query). A workspace whose
  members have all lost tenant access becomes inaccessible (acceptable; superuser can intervene).
- **Enforcement hardening:** centralized authorizer + soft-delete default manager + CI fitness test
  + one-time audited sweep.

## Design

### 1. Full-sync refresh (`tenant_resolution.py`)

A shared helper the three `resolve_*` functions call:

```
_sync_memberships(user, provider, connection, fresh_tenants):
    # fresh_tenants = the tenants returned by a SUCCESSFUL upstream fetch.
    # NOTE: use all_objects throughout — the default manager hides archived rows,
    # so get_or_create on `objects` would create a DUPLICATE (violating unique(user,
    # tenant)) when a previously-revoked membership exists. We must see it to un-archive it.
    for t in fresh_tenants:                       # additive: add new + un-archive returning
        tm = all_objects.get_or_create(user, t); tm.connection = connection
        tm.archived_at = None; save
    # revoke: archive live memberships for THIS (user, provider, connection)
    # whose tenant is not in fresh_tenants
    for tm in all_objects.filter(user, tenant.provider=provider, connection=connection,
                                 archived_at__isnull=True).exclude(tenant in fresh_tenants):
        tm.archived_at = now; save
```

- **Fail-open guard:** archival runs **only after a fully successful fetch**. The fetch already
  `raise_for_status()`s and raises `ConnectAuthError`/`OCSAuthError`/`CommCareAuthError` on 401/403,
  so any error aborts before the archive step — a transient upstream blip never mass-revokes.
- **Strict scope:** only `(user, provider, connection)`. An OAuth refresh never touches another
  provider or an API-key connection's memberships.

### 2. Triggers

- **Share-time (the missing piece).** In `WorkspaceMemberListView.post`, before failing the tenant
  check, **force-refresh the target** for the workspace's tenant provider(s) using the *target's*
  stored token (the `async_to_sync` + sync-token-fetch pattern the login signal already uses),
  bypassing the self-refresh TTL, under a bounded timeout. Then re-check via the authorizer.
- **Self (existing).** `tenant_list_view`'s refresh now also revokes, because it calls the same
  `resolve_*` (now full-sync). An active user's own activity applies revocation within the refresh
  TTL. (TTL is the tuning knob for revocation promptness; pick a value in the plan.)
- **Periodic.** Out of scope; follow-up.

### 3. Centralized authorizer — `apps/workspaces/access.py` (NEW)

The single source of truth. Sync **and** async variants:

```
resolve_workspace_access(user, workspace_id) -> (workspace, WorkspaceMembership) | denial
    rule: WorkspaceMembership(user, workspace) exists
          AND ( workspace has 0 tenants
                OR exists live TenantMembership(user, t) for some t in workspace.tenants )
```

- Zero-tenant workspace → `WorkspaceMembership` suffices (no tenant data to gate).
- All existing resolvers delegate here: `resolve_workspace`, `resolve_workspace_drf`,
  `aresolve_workspace`, chat `_resolve_workspace_and_membership` (closing the multi-tenant hole),
  MCP `_resolve_workspace_memberships`.
- The add-member check reuses the same "shares ≥1 live tenant" predicate, so add and runtime agree.
- Non-destructive: `WorkspaceMembership` rows persist; access auto-restores **only** when an upstream
  re-grant un-archives the tenant membership.

### 4. Soft-delete default manager (`TenantMembership`)

- `objects` (default) excludes `archived_at IS NOT NULL` — archived (= revoked) memberships are
  invisible everywhere by default.
- `all_objects` includes archived; used **only** by: the resolver's un-archive step, the Connections
  UI (which already filters explicitly), admin, the merge service, and migrations.
- `Meta.base_manager_name = "all_objects"` so related-manager lookups, cascades, and the merge
  service behave predictably; the default `objects` is the safe one for access reads.
- Part of the audited sweep: switch the call sites that legitimately need archived rows to
  `all_objects`; everything else gets safe-by-default behavior for free.

### 5. CI fitness test

Mirrors the repo's existing enforce-async-ORM check. Fails CI if workspace-access resolution or the
live-tenant predicate appears outside `apps/workspaces/access.py` (allowlist: the authorizer module
and its tests). This is what makes a new endpoint that forgets the authorizer **un-mergeable**,
rather than relying on a reviewer to notice.

### 6. Error UX at share time

Replace the single opaque error with the real reason after the refresh attempt:

- target gained access → `201` success;
- Scout user, refreshed, still no live access → "X doesn't have access to this opportunity in
  Connect.";
- no usable token / no Connect connection → "Ask X to sign into Scout again (Connections → Connect).";
- no Scout user → existing "No Scout user with that email." (Case C).

### 7. One-time audited sweep

Enumerate **every** workspace/tenant access site — DRF views, async views, chat, the MCP server,
the materializer, and tasks — and route each through the authorizer / live manager. Run as a
parallel mapping pass, then verified by the fitness test (§5). Converts "hope we found them all"
into "enumerated, and CI keeps it that way."

## Testing

- `_sync_memberships`: additive add, archive-absent, **fail-open on fetch error**, scope-to-connection.
- Authorizer: live / archived-only / no-membership / zero-tenant cases.
- Soft-delete manager: default hides archived, `all_objects` sees them, related-manager + cascade
  behavior, merge-service still works.
- Share-time refresh: target gains access → success; still missing → correct error; dead token →
  fallback message. (Mock the upstream fetch.)
- Each gate denies an archived-only user; the multi-tenant hole is closed.
- Fitness test catches a deliberately-bypassing fixture.

## Risks & edges

- **Orphaned workspace** (all managers lost access) — accepted; superuser intervention.
- **Share-time latency** — the export can be large (e.g. mtheis ≈ 459 opps); bounded timeout +
  fallback; revisit async if it proves slow.
- **Dead-token targets** (the 5 pre-2026-06-04 orphan accounts, or nlesh if his refresh token has
  expired) — can't refresh server-side; fall back to "re-login." Same remedy as today.
- **Org-membership vs OpportunityAccess** — Connect's export only returns org-membership opps, so
  "grant access in Connect" must mean adding the user to the org, not just opportunity-level access.
- **Connect propagation delay** after a re-grant — share-time retry covers it.
- **Soft-delete manager gotchas** — mitigated by `base_manager_name` + explicit related-manager and
  cascade tests.

## Rollout

Code-only (no schema change — `archived_at` already exists; the manager change is code). Likely
split in the implementation plan into: (1) full-sync resolver + share-time trigger, (2) authorizer +
soft-delete manager + audited sweep, (3) fitness test. Final sequencing decided in the plan.
