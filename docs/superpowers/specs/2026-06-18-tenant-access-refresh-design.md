# Tenant-access refresh + live enforcement (Root Cause A)

**Date:** 2026-06-18
**Status:** Design — reviewed (Fable adversarial pass folded in)
**Branch:** `bdr/tenant-access-refresh`

## Problem

Scout learns "user X belongs to opp/domain/team Y" **only** when X personally completes an
OAuth login. `resolve_commcare_domains` / `resolve_connect_opportunities` /
`resolve_ocs_chatbots` (`apps/users/services/tenant_resolution.py`) run from
`resolve_tenant_on_social_login` (`apps/users/signals.py`) at login, and from
`tenant_list_view` (`apps/users/views.py`) for the **currently logged-in** user on a TTL cache.

Two consequences:

1. **Grant staleness.** When X is added to an opp in Connect *after* their last Scout login, Scout
   never finds out until X logs in again. A manager (B) sharing a workspace hits the
   `TenantMembership` check (`apps/workspaces/api/workspace_views.py`) against X's frozen data and
   fails. Scout never refreshes **X** (the target) — only the sharer refreshes themselves. This is
   the original nlesh failure.

2. **Revocation gap.** The resolver is **additive-only** (`aget_or_create` + un-archive; never
   archives lost access). When upstream removes access, Scout keeps serving it indefinitely.

Underlying both: access is gated **inconsistently**.

| Gate | Checks | Honors tenant access? |
|---|---|---|
| `resolve_workspace` / `_drf` / `aresolve_workspace` | `WorkspaceMembership` only | ❌ never |
| chat `_resolve_workspace_and_membership` — multi-tenant | `WorkspaceMembership` only | ❌ tenant check skipped |
| chat — single-tenant | `TenantMembership.aget` | ⚠️ no `archived_at` filter |
| MCP `_resolve_workspace_memberships` | `TenantMembership` by tenant+user | ⚠️ no `archived_at` filter |

Even archiving a membership would not reliably revoke access, and a multi-tenant workspace member
can currently query data with **no** tenant membership at all.

## Goals

- **Grant:** reflect newly-granted upstream access promptly — especially at share time — without the
  target manually reconnecting.
- **Revocation:** when upstream removes access, stop serving it to **every** Scout user and manager,
  with **no human-in-Scout path to reinstate it**.
- **Robustness:** make the access rule structurally un-missable — one authorizer, safe-by-default
  reads, and a CI check that fails if anything bypasses it.

## Non-goals (explicit)

- **C — invite / pre-share for non-Scout users.** Separate.
- **Periodic background sweep.** Activity-triggered refresh bounds staleness for now; cron is a
  follow-up.
- **Content soft-delete / recovery (Case 1, below).** Out of scope; explicitly fenced off.
- **Per-tenant query scoping inside a multi-tenant workspace** (see Risk R1). Follow-up.

## Two distinct meanings of "archived" — never share a code path

| | What it is | Who can revive it |
|---|---|---|
| **Case 1 — content soft-delete** | a user deletes their *own* work (workspace, artifact, recipe) | recoverable by a person in Scout (undo) |
| **Case 2 — access revoked upstream** | Connect/HQ/OCS removed the user from a tenant | **no one in Scout** — only (a) upstream re-grant → Scout re-syncs, or (b) a superuser at the DB |

This design touches **only Case 2** (the `TenantMembership` access layer). An archived
`TenantMembership` is a **tombstone, not an undo**: no user/manager/admin affordance reinstates it.
The only un-archive paths are the resolver reflecting an upstream re-grant, and DB surgery. No Case-1
recovery code may ever flip a `TenantMembership`'s archived state.

## Locked decisions

- **Scope:** grant + revocation (full sync).
- **Revocation model:** live enforcement — compute effective access; `WorkspaceMembership` rows are
  non-destructive ("was invited"); effective access is recomputed.
- **Manager gating:** unified — no live tenant ⇒ no workspace (manage *or* query). Accept orphaned
  workspaces (superuser can intervene).
- **Enforcement hardening:** centralized authorizer + soft-delete default manager + CI fitness test
  + one-time audited sweep.

---

## Design

### 1. Full-sync refresh (`tenant_resolution.py`) — provider-aware archival

A shared `_sync_memberships(user, provider, connection, fresh_tenants, *, archive_scope)`:

```
# Use all_objects THROUGHOUT: the default manager hides archived rows, so
# get_or_create on `objects` would create a DUPLICATE (unique(user,tenant)) when a
# revoked row exists. We must see tombstones to un-archive them.
for t in fresh_tenants:                              # additive: add new + un-archive
    tm = all_objects.get_or_create(user, t); tm.connection = connection
    tm.archived_at = None; save
# revoke: archive live memberships in archive_scope whose tenant is not in fresh_tenants
for tm in all_objects.filter(archive_scope, archived_at__isnull=True).exclude(tenant in fresh_tenants):
    tm.archived_at = now; save
```

**`archive_scope` is per-provider and deliberately conservative — this is where the review's
mass-revoke footguns live:**

- **Connect** — scope `(user, provider="commcare_connect", connection)`. The export
  (`commcare-connect data_export/views.py:216`) is unpaginated and `export`-scope-gated (wrong scope
  → 403 → raises), so a complete 2xx is genuinely the full set.
- **CommCare (B1)** — **do not enable archival until pagination is truncation-safe.**
  `_fetch_all_domains` currently only follows an *absolute* `meta.next`; a relative `next` silently
  stops after page 1. Fix: follow relative `next` (urljoin against the base) **and raise if a
  `next` is present but unfollowable** — a truncated fetch must not look "successful." Only then may
  CommCare archive scope `(user, provider="commcare", connection)`.
- **OCS (B2)** — OCS tokens are **team-scoped**; a successful `/api/experiments/` fetch returns only
  the *current team's* chatbots. Scope archival to `(user, provider="ocs", connection,
  provider_metadata__team_slug = <current token team>)`. **Never** archive other teams' memberships.

**Fail-open guards:**
- Archival runs **only after a fully successful fetch**; every resolver already
  `raise_for_status()`s and raises `*AuthError` on 401/403 **before any membership write**
  (verified: `tenant_resolution.py:86-90, 145-149, 192-196`).
- **Shape-drift guard (S3):** require the expected key to be **present** in the 2xx body
  (`"opportunities"` / `"objects"` / `"results"`) — a `KeyError` aborts. Never `.get(key, [])`,
  which would treat a renamed/malformed payload as "user has zero tenants" and archive everything.

### 2. Triggers

- **Share-time (the missing piece).** In `WorkspaceMemberListView.post`, before failing the tenant
  check, **force-refresh the target** for the workspace's tenant provider(s), using the *target's*
  stored token, then re-check via the authorizer.
  - Token must be **refresh-aware (S4):** stale targets — the exact users this feature serves —
    have expired access tokens. Use the `token_needs_refresh` → `refresh_oauth_token`
    (`apps/users/services/token_refresh.py`) path (as `auth_views` already does), not the raw
    `_aget_token_value`. `aget_social_token(user, provider)` fetches an arbitrary user's token.
  - Pattern: `async_to_sync(resolve_*)(target, token)` from the sync DRF view (proven at
    `signals.py:66`).
  - **Explicit bounded timeout** (target: 8s, well under the resolver's 30s httpx timeout) so a slow
    export never ties up a sync DRF worker; on timeout/refresh-failure → fall through to the error UX.
- **Self (existing).** `tenant_list_view`'s refresh now also revokes (same `resolve_*`, now
  full-sync). Active users get revocation within the refresh TTL of their own activity. TTL value
  chosen in the plan.
- **Periodic.** Out of scope; follow-up.

### 3. Centralized authorizer — `apps/workspaces/access.py` (NEW)

Single source of truth, sync **and** async:

```
resolve_workspace_access(user, workspace_id) -> (workspace, WorkspaceMembership) | denial
    rule: WorkspaceMembership(user, workspace) exists
          AND ( workspace has 0 tenants
                OR exists live TenantMembership(user, t) for some t in workspace.tenants )
```

- Zero-tenant workspace → `WorkspaceMembership` suffices (no tenant data to gate).
- All resolvers delegate here: `resolve_workspace`/`_drf`/`aresolve_workspace`, chat
  `_resolve_workspace_and_membership` (closes the multi-tenant hole), and the add-member check reuses
  the same "shares ≥1 live tenant" predicate.
- **MCP `_resolve_workspace_memberships` has a dual role (N2):** with `user_id` it's an access check
  (route through the authorizer); with `user_id` empty it **enumerates all members' credentials for
  materialization** — that path must be preserved and is *not* a per-user access check. Split the two
  uses explicitly.

### 4. Soft-delete default manager (`TenantMembership`) — corrected

- `objects` (default) excludes `archived_at IS NOT NULL`; `all_objects` includes everything.
- `Meta.base_manager_name = "all_objects"` — **this only protects cascade collection**, NOT reverse
  related managers. **Correction to the earlier draft (B4):** reverse managers
  (`user.tenant_memberships`, `connection.memberships`, `tenant.memberships`) build from the related
  model's default manager, so they **will hide archived rows** after this change. Every reverse-manager
  and upsert site must be audited and switched to `all_objects` where it needs tombstones:
  - `apps/users/views.py:64` `_persist_api_key_connection` — `get_or_create` upsert → **must** use
    `all_objects` or re-adding an API key after revocation `IntegrityError`s.
  - `apps/users/services/merge.py:163-167,191` — `_merge_tenant_memberships` builds
    `canonical_tenant_ids` from `objects` (misses archived), and `conn.memberships.update(...)` would
    skip tombstones. Use `all_objects`, and add **live-beats-tombstone conflict semantics (B3):** when
    canonical and duplicate both have a row for a tenant, keep the **live** one, not blindly canonical's.
  - `apps/users/views.py:198,285,333` (`connection.memberships`) and `apps/transformations/views.py`
    + `serializers.py` (`user.tenant_memberships` ACL) — audit; most *want* the live-only default,
    but confirm per site.
- **Do not** set `use_in_migrations` (N1): historical models via `apps.get_model` don't inherit
  custom managers, so `0004`/`0007` are unaffected. Remove "migrations" from the escape-hatch list.

### 5. CI fitness test — new machinery (not a mirror)

**Correction (S2):** there is no existing "enforce-async-ORM" source scanner to mirror — only the
ruff `ASYNC` ruleset and `tests/test_ci_integrity.py` (an env-var assertion). This is **new**. Define
the banned pattern **precisely** to avoid a giant allowlist: *authorization-style* filtering — a
`WorkspaceMembership.objects.{get,filter,...}(... user=<request user> ...)` or the live-tenant
predicate — in any view/tool/consumer module **outside** `apps/workspaces/access.py`. Allowlist: the
authorizer, its tests, and the enumerated legitimate non-auth uses. A pytest AST/grep check that
fails CI on a new bypass.

### 6. Error UX at share time

After the refresh attempt: target gained access → `201`; Scout user, refreshed, still no live access
→ "X doesn't have access to this opportunity in Connect."; no usable/refreshable token → "Ask X to
sign into Scout again (Connections → Connect)."; no Scout user → existing "No Scout user" (Case C).

### 7. One-time audited sweep — expanded inventory (S1)

Route **every** workspace/tenant access site through the authorizer / live manager. Beyond the four
gates in the table above:

- `apps/workspaces/api/workspace_views.py:173` (workspace-create tenant validation), `:573`
  (add-tenant-to-workspace) — no `archived_at` filter today.
- `apps/workspaces/api/views.py:343` (schema-refresh picks a membership for materialization).
- `apps/transformations/views.py` + `serializers.py` (tenant-scoped asset ACL via
  `user.tenant_memberships`).
- `apps/recipes/services/runner.py:111` (recipe-runner credential membership).
- `apps/workspaces/tasks.py:139` (materialization loads membership by id; `:231` already filters).
- `apps/users/views.py:159,378` (tenant select / ensure — embed-SDK path).
- `apps/workspaces/permissions.py` — `IsWorkspaceMember`/`IsWorkspaceManager` are **dead code** and a
  ready-made authorizer bypass for the next endpoint: **rewrite atop the authorizer or delete.**

Most gates get the archived filter for free from §4; several also need the *authorizer* (workspace +
tenant), not just the filter. The fitness test's allowlist enumerates the legitimate non-auth uses.

## Testing

- `_sync_memberships`: additive add; archive-absent; **fail-open on fetch error**; **shape-drift
  KeyError aborts**; per-provider `archive_scope` (Connect all; CommCare only when pagination-safe;
  **OCS never touches other teams**).
- CommCare pagination: relative `next` followed; unfollowable `next` **raises** (no silent truncation).
- Authorizer: live / archived-only / no-membership / zero-tenant.
- Soft-delete manager: default hides archived; `all_objects` sees them; **reverse managers**
  (`conn.memberships`, `user.tenant_memberships`) behave as audited; cascade on connection delete;
  merge **live-beats-tombstone**; API-key re-add after revocation does not `IntegrityError`.
- Share-time: target gains access → success; still missing → correct error; **expired token is
  refreshed**; dead token → fallback.
- Each gate denies an archived-only user; multi-tenant hole closed; MCP credential-enumeration path
  preserved.
- Fitness test catches a deliberately-bypassing fixture.

## Risks & edges

- **R1 — multi-tenant partial-revocation leak (S5).** The authorizer grants full workspace access on
  **any one** live tenant, and a multi-tenant workspace's unioned view schema exposes all tenants'
  rows. Losing tenant A while keeping B still lets the user query A's data. Matches today's add-member
  semantics; **accepted as a documented residual** — per-tenant query scoping is a follow-up.
- **R2 — public share links (S6).** `PublicRecipeRunView` (`apps/recipes/api/views.py:169`,
  `AllowAny`) serves shared output by token regardless of membership. Pre-existing; acknowledged, not
  closed here.
- **Orphaned workspace** (all managers revoked) — accepted; superuser intervention. `_is_last_manager`
  counts by `WorkspaceMembership` only, and `WorkspaceDetailView.delete`'s last-tenant rule can strand
  a revoked user with an undeletable workspace (N4) — acceptable.
- **Share-time latency** — bounded timeout (§2) + fallback.
- **Dead-token targets** (the pre-2026-06-04 orphans; nlesh if his refresh token expired) — can't
  refresh; fall back to "re-login."
- **Org-membership vs OpportunityAccess** — Connect's export returns only org-membership opps, so
  "grant in Connect" must mean org membership.

## Rollout

Code-only (no schema change — `archived_at` exists; manager change is code). Plan will sequence into:
(1) provider-aware full-sync + share-time trigger; (2) soft-delete manager + reverse-manager/upsert
audit + merge conflict semantics; (3) centralized authorizer + audited sweep + `permissions.py`;
(4) fitness test. Each independently testable.
