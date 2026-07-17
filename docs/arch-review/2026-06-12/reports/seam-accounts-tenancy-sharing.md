# Seam Review: Accounts ↔ Tenancy ↔ Workspace Sharing

*Reviewer: seam:accounts-tenancy-sharing. Mandate: own the chain
User → TenantConnection → TenantMembership → Tenant → WorkspaceTenant → Workspace →
WorkspaceMembership as ONE contract. Where can the chain produce two answers to
"what can this user touch"?*

Repo HEAD: `35e4230`, branch `main`. Report-only; no code changed.

---

## The contract, as built

There are **two parallel notions of access** in this codebase, and they are resolved by
different code with different rules:

1. **Tenant access** = `TenantMembership(user, tenant)`. Created by provider resolution
   (`tenant_resolution.py`) or API-key persistence (`users/views.py`). This is the
   *provider-authorized* answer: "the provider says this user may see this tenant's data."
   It also carries the credential pointer (`connection`) used to *materialize* data.

2. **Workspace access** = `WorkspaceMembership(user, workspace, role)`. This is what every
   read/query path actually checks (`workspace_resolver.py`, `chat/helpers.py`,
   `artifacts/views.py`). A `Workspace` fans out to 1..N tenants via `WorkspaceTenant`.

The seam is the join between these two. For a **single-tenant** workspace they are kept
roughly aligned (chat requires a `TenantMembership` for the sole tenant). For a
**multi-tenant** workspace they deliberately diverge: `WorkspaceMembership` alone is
sufficient, and the physical query surface (`WorkspaceViewSchema`) `UNION`s **every**
constituent tenant. That divergence is the source of most findings below.

Three more structural facts matter:

- **Tenant data schemas are shared across workspaces and across users.**
  `SchemaManager.provision` keys the physical schema purely on a *sanitized
  `tenant.external_id`*, and explicitly returns an existing schema regardless of which
  `Tenant` row asks for it (`schema_manager.py:57-129`). Data persists in Postgres
  independently of any membership row.
- **Tenant resolution is additive-only.** `resolve_commcare_domains` /
  `resolve_connect_opportunities` / `resolve_ocs_chatbots` only ever un-archive/upsert the
  tenants the provider *currently* returns; nothing ever archives a membership the provider
  has *stopped* returning.
- **Workspace membership outlives the tenant membership that justified it.** Removing a
  tenant from a workspace, or archiving/deleting a user's `TenantMembership`, never prunes
  the corresponding `WorkspaceMembership`.

---

## Findings

### F1 — Multi-tenant workspace membership grants read access to tenants the user was never authorized for (BROKEN-NOW / security)

**Confidence: verified-by-trace** (intent is partly ambiguous — see "essential vs accidental").

The add-member gate requires the invitee to share **at least one** tenant with the
workspace, then grants access to **all** of them.

Chain:

1. Manager adds member. The only tenant check is "shares ≥1 tenant":
   `apps/workspaces/api/workspace_views.py:411-419`
   ```python
   workspace_tenant_ids = workspace.workspace_tenants.values_list("tenant_id", flat=True)
   shares_tenant = TenantMembership.objects.filter(
       user=target, tenant_id__in=workspace_tenant_ids
   ).exists()
   if not shares_tenant:
       return Response({"error": "User is not part of this workspace's tenants."}, ...403)
   ```
   A member who has `TenantMembership` for **T1 only** is admitted to a workspace whose
   tenants are **{T1, T2}**.

2. That member chats. The multi-tenant branch returns success with **no per-tenant check**:
   `apps/chat/helpers.py:106-108`
   ```python
   is_multi_tenant = await workspace.workspace_tenants.acount() > 1
   if is_multi_tenant:
       return workspace, None, True
   ```
   and `apps/chat/views.py:113-114` skips the tenant guard precisely when `is_multi_tenant`:
   ```python
   if tm is None and not is_multi_tenant:
       return JsonResponse({"error": "No tenant membership for this workspace"}, status=403)
   ```

3. The agent's `query` tool resolves the workspace to the **view schema**, which is a
   `UNION ALL` over every constituent tenant's tables:
   `mcp_server/context.py:113-139` (`load_workspace_context` multi-tenant branch) →
   `apps/workspaces/services/schema_manager.py:367-405` (`build_view_schema` creates
   `{prefix}__{table}` views for **all** tenants and grants the view's `_ro` role
   `SELECT` on every constituent tenant schema).

4. Query executes under that role with the view schema on the search path:
   `mcp_server/services/query.py:44-50` (`SET ROLE {schema}_ro`). No row-level or
   per-tenant filtering exists anywhere in the path.

Consequence: a user with provider authorization for T1 reads **T2's data** (which a
different manager/user materialized) simply by being a workspace member. The same is true
for the `get_metadata` / `describe_table` / `list_tables` tools and `get_lineage`.

**reachable_via:** `POST /api/workspaces/<id>/members/` then `POST /api/chat/`. Live today.

**Essential vs accidental:** *mixed.* It is plausibly **intended** that a workspace is a
shared data surface and that `WorkspaceMembership` is the access boundary (the data-sources
tab, the "shares ≥1 tenant" check, and the view schema all read that way). If so, the bug
is that the gate only requires **one** shared tenant rather than **all** of the
workspace's tenants — i.e., the gate's strength does not match the breadth of access it
confers. Either way the chain gives two answers: tenant resolution says "T1 only," the
workspace path says "T1 and T2."

---

### F2 — `provision()` shares one physical schema across distinct tenants that sanitize to the same name (LATENT / security, cross-tenant exposure + corruption)

**Confidence: strong-inference** (mechanism verified by trace; trigger requires a name collision).

`_sanitize_schema_name` is lossy and unbounded — lowercases, maps `-`→`_`, strips
non-alphanumerics, prefixes `t_` if it starts with a digit — and `TenantSchema.schema_name`
is `unique=True`. `provision()` then returns an existing schema **keyed only on
`schema_name`, ignoring the tenant FK**:

`apps/workspaces/services/schema_manager.py:625-631`
```python
def _sanitize_schema_name(self, tenant_id: str) -> str:
    name = tenant_id.lower().replace("-", "_")
    name = "".join(c for c in name if c.isalnum() or c == "_")
    if name and name[0].isdigit():
        name = f"t_{name}"
    return name or "unknown"
```
`apps/workspaces/services/schema_manager.py:66-78`
```python
schema_name = self._sanitize_schema_name(tenant.external_id)
existing = TenantSchema.objects.filter(
    schema_name=schema_name, state__in=[ACTIVE, MATERIALIZING],
).first()                       # <-- no tenant filter
if existing:
    self._ensure_physical_schema(schema_name)
    existing.touch()
    return existing             # <-- returns ANOTHER tenant's schema row
```

Collision examples that map to the same `schema_name`:
- CommCare domain `42` and Connect opportunity `42` both → `t_42` (no provider namespacing
  in the schema name).
- CommCare domains `Foo-Bar` and `foo_bar` both → `foo_bar`.

When tenant B's materialization calls `provision(B)` and a colliding tenant A already has an
ACTIVE schema, B's loader writes B's data into A's physical schema
(`materializer.py:183` → `SchemaManager().provision(tenant_membership.tenant)`), and B never
gets its own `TenantSchema` row. A's single-tenant workspace then serves B's data, because
`load_tenant_context` resolves by `tenant__external_id` → finds A's row → reads the now
co-mingled schema (`mcp_server/context.py:56-66`).

This is the same input-validation family as the already-fixed 63-byte view-name truncation
(`build_view_schema` Step 1/2), but it lives one layer down at the **base tenant schema
name**, where there is no collision check at all — `build_view_schema` guards view-name
collisions; `provision` guards none.

**reachable_via:** any materialization of a tenant whose sanitized external_id collides with
an existing ACTIVE tenant schema. Cross-provider integer/domain collisions make this more
than theoretical.

**Essential vs accidental:** *accidental.* A per-tenant uniqueness key (e.g.
`{provider}_{hash(external_id)}`) or filtering `provision` by `tenant=` removes it.

---

### F3 — Tenant resolution never revokes access lost upstream; workspace data stays readable (LATENT→BROKEN-NOW / security)

**Confidence: verified-by-trace** (additive-only resolution) + **strong-inference** (no reaper exists).

`resolve_*` functions only upsert and **un-archive** memberships for tenants the provider
currently returns; they never archive memberships for tenants that have **disappeared** from
the provider response:

`apps/users/services/tenant_resolution.py:51-61` (CommCare), `:98-108` (Connect),
`:155-167` (OCS) all follow the pattern:
```python
tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
tm.connection = conn
tm.archived_at = None          # only ever cleared, never set here
await tm.asave(...)
```

A repo-wide search for `archived_at` writes confirms the **only** archival sites are
user-initiated: `disconnect_provider_view` (`auth_views.py:203-205`) and connection delete
(`users/views.py:286`). There is no periodic reconciliation comparing provider-current
tenants against stored memberships.

Consequence: when a user is removed from a CommCare domain / Connect opportunity / OCS team
upstream, their `TenantMembership` survives, they keep appearing in
`tenant_list_view`, and — crucially — they retain **read access to already-materialized
data** for that tenant through any workspace, because the physical schema and
`WorkspaceMembership` are untouched. The OAuth token is still valid (it is the user's token,
scoped by the provider), so even a *re-materialization* may partially succeed depending on
provider-side per-resource enforcement.

**reachable_via:** normal login refresh after upstream access removal. The data read is live
today; only the (eventual) provider-side rejection of new materialization is uncertain.

**Essential vs accidental:** *accidental.* A reaper that archives memberships absent from the
latest provider response closes it.

---

### F4 — Authorization paths disagree on whether an archived `TenantMembership` still counts (BROKEN-NOW / correctness, weak security)

**Confidence: verified-by-trace.**

`archived_at` is honored inconsistently across the chain — the same membership is "active"
to some gatekeepers and "gone" to others:

| Path | Filters `archived_at`? | File |
|---|---|---|
| `tenant_list_view` (UI list) | **yes** (`archived_at__isnull=True`) | `users/views.py:124-126` |
| `materialize_workspace` (credentials) | **yes** | `apps/workspaces/tasks.py:231-239` |
| chat single-tenant guard | **no** — `aget(user=user, tenant=tenant)` | `apps/chat/helpers.py:114-119` |
| MCP `_resolve_workspace_memberships` (run_materialization authz) | **no** | `mcp_server/server.py:509` |

After `disconnect_provider_view`, a single-tenant workspace's membership row still exists
(archived, `connection=None`). The chat guard at `chat/helpers.py:116-119` does an
unfiltered `aget`, so it **passes**, and the user can keep querying the already-materialized
single-tenant schema. `aresolve_credential` would return `None` for that membership
(`connection is None`, `credential_resolver.py:75-77`), so re-materialization fails — but
reads of existing data do not need a credential.

So "disconnected" means different things in different places: invisible in the tenant list,
unusable for materialization, yet still sufficient to authorize chat/query.

**reachable_via:** disconnect a provider, then open a single-tenant workspace chat. Live.

**Essential vs accidental:** *accidental.* One shared helper that consistently filters
`archived_at__isnull=True` resolves it.

---

### F5 — Removing a tenant from a workspace orphans members who shared only that tenant (LATENT / security)

**Confidence: verified-by-trace.**

`remove_workspace_tenant` reconciles the **view schema** but never re-examines
`WorkspaceMembership`:

`apps/workspaces/services/workspace_service.py:33-71` — deletes the `WorkspaceTenant`,
marks/rebuilds/teardowns the view schema, and returns. No membership pruning.

Combined with F1: a member admitted because they shared T1 keeps full `WorkspaceMembership`
after a manager removes T1, so they now share **zero** of the workspace's tenants yet retain
access to the remaining ones (T2, T3, …). The "shares ≥1 tenant" invariant the add-gate
pretends to enforce (`workspace_views.py:411-419`) is therefore only checked once, at add
time, and silently decays.

**reachable_via:** add member sharing T1 → manager removes T1 → member still reads T2.

**Essential vs accidental:** *accidental.*

---

### F6 — Artifact live-query and chat resolve a multi-tenant workspace to *different* schemas (LATENT / correctness)

**Confidence: verified-by-trace.**

For the *same* workspace, two read surfaces route to two different physical schemas:

- Chat / agent `query`: `load_workspace_context` → **view schema** for multi-tenant
  (`mcp_server/context.py:104-139`).
- Artifact `query-data`: `load_tenant_context(workspace.tenants.afirst().external_id)` →
  the **first tenant's** schema only (`apps/artifacts/views.py:795-800`).

So in a multi-tenant workspace, an artifact's `source_queries` run against a single tenant's
raw schema, not the unioned view schema. If the SQL was written by the agent against view
names (`{prefix}__{table}`) it fails ("does not exist"); if it uses bare table names it
silently returns only the first tenant's slice. This is the same "two answers" pattern at
the artifacts↔tenancy seam, and it is a data-correctness footgun (partial/empty results
presented as authoritative). The `export` path (`views.py:957`) and the sandbox
`query-data` fetch (`views.py:254`) share the assumption.

**reachable_via:** create an artifact with live queries in a 2+ tenant workspace, open it.

**Essential vs accidental:** *accidental* — artifact context resolution predates multi-tenant
view schemas and was never updated to call `load_workspace_context`.

---

### F7 — User-merge resolves cross-account state but cannot run for the most common duplicate case (DEBT / correctness)

**Confidence: verified-by-trace** (merge logic) + **strong-inference** (operational gap).

`merge_users` is careful and correct for the cases it runs: it repoints SocialAccount,
dedupes EmailAddress with primary normalization, dedupes `TenantMembership` by tenant,
merges `TenantConnection` honoring the one-OAuth-per-(user,provider) constraint, and merges
`WorkspaceMembership` taking the **higher** role (`merge.py:155-226`). The long-tail FK
sweep (`_repoint_long_tail_fks`) is a reasonable forward-compatible design.

But the auto-merge trigger only fires when (a) the logging-in OAuth user has **no email yet**
and (b) the canonical user owns a **verified** `EmailAddress` for the incoming email:

`apps/users/signals.py:94-115`
```python
if user.email:
    return                      # already has an email — never reconciled
...
canonical_owns_email = EmailAddress.objects.filter(
    user=canonical, email__iexact=new_email, verified=True
).exists()
if not canonical_owns_email:
    logger.warning("Refusing auto-merge: ...")
    return
```

Per project memory, several providers (notably Connect) return an email but never produce a
*verified* `EmailAddress`, so the refusal branch is the steady state. The refusal is
secure-by-design, but the consequence for *this* seam is that one human ends up with **two
User rows**, and their `TenantMembership`s/`WorkspaceMembership`s are **split across both** —
producing exactly the "two answers to what can this user touch" the mandate targets
(different login → different set of workspaces). The operator command
`merge_duplicate_users` is the only remedy and is manual.

**reachable_via:** log in via two providers that both yield the same human but no verified
EmailAddress. Live; resolved only operationally.

**Essential vs accidental:** *essential* (identity reconciliation across unverifying IdPs is
genuinely hard) — but the silent split with no surfaced "possible duplicate" signal is
accidental.

---

## What's fine

- **Workspace creation tenant validation.** `WorkspaceListView.post` validates every
  requested `tenant_id` against the requester's own `TenantMembership`s before creating
  `WorkspaceTenant` rows (`workspace_views.py:172-184`). You cannot create a workspace over
  tenants you lack.
- **Add-tenant requires requester's own access.** `WorkspaceTenantView.post` checks
  `TenantMembership.objects.filter(user=request.user, tenant=tenant)` before delegating to
  `add_workspace_tenant` (`workspace_views.py:572-579`), so multi-tenant workspaces are
  bounded by the union of managers' own tenant access. (Members reading that union is F1; the
  *composition* of the union is sound.)
- **Last-manager guards.** Demotion and removal both prevent orphaning a workspace
  (`workspace_views.py:26-30, 477-486, 509-514`).
- **Thread/recipe share tokens are unguessable.** `secrets.token_urlsafe(32)` with the
  `is_shared/is_public ↔ token` invariant maintained in `save()`
  (`chat/models.py:41-48`, `recipes/models.py:116-120`). Public thread view exposes only
  static messages + static artifact `data/code`, not the live `query-data` endpoint
  (which requires `aresolve_workspace`, `artifacts/views.py:778`).
- **Cross-workspace/foreign-thread rejection.** `chat/views.py:121-137` rejects POSTs to a
  thread owned by another user or workspace; `thread_messages_view` distinguishes new vs
  stale threads (`thread_views.py:146-156`).
- **OCS team fail-closed.** `_oauth_team_mismatch` refuses an OAuth token that has moved to a
  different OCS team than the membership recorded (`credential_resolver.py:52-95`).
- **MCP `run_materialization` defense-in-depth.** Re-checks workspace membership and thread
  ownership before binding a ThreadJob (`server.py:553-570`).
- **DRF role permission classes are internally correct** — but see coverage note: whether
  routes actually attach them was not exhaustively verified here.

---

## Coverage log

**Deep-read (line-by-line):**
- `apps/users/models.py`, `apps/workspaces/models.py`, `apps/workspaces/permissions.py`
- `apps/users/views.py`, `apps/users/services/tenant_resolution.py`,
  `apps/users/services/credential_resolver.py`, `apps/users/services/merge.py`,
  `apps/users/signals.py`, `apps/users/apps.py`
- `apps/workspaces/api/workspace_views.py`, `apps/workspaces/workspace_resolver.py`,
  `apps/workspaces/services/workspace_service.py`
- `apps/workspaces/services/schema_manager.py` (provision, view-schema build/teardown,
  role mgmt, sanitize)
- `mcp_server/context.py`, `mcp_server/services/query.py`
- `apps/chat/helpers.py` (resolve), `apps/chat/views.py` (authz portion, lines ~95-225),
  `apps/chat/thread_views.py`, `apps/chat/models.py`
- `mcp_server/server.py` tools relevant to the seam (query/get_metadata/get_schema_status/
  run_materialization/teardown/_resolve_workspace_memberships)
- `apps/workspaces/tasks.py` `refresh_tenant_schema` + `materialize_workspace` (lines 120-360)
- `apps/artifacts/views.py` authz + `query-data` (grep-mapped; lines 681-822 read)

**Skimmed (grep / partial):**
- `apps/users/auth_views.py` (disconnect + onboarding flags only)
- `apps/recipes/api/views.py` + `recipes/models.py` (share-token surfaces only)
- `mcp_server/services/materializer.py` (only the `provision`/`tenant_membership` call sites)
- `apps/agents/graph/base.py` (only MCP tool-name/param-injection region, lines 60-145, 396-575)

**NOT examined (in-scope, left for gap loop):**
- `apps/users/services/ocs_team.py` (TODO(OCS #3586) stopgap) — team detection correctness
  and its effect on `_oauth_team_mismatch` fail-closed guard not traced end-to-end.
- `apps/users/services/api_key_providers/*` and `token_refresh.py` — credential packing,
  rotation `verify_for_tenant`, and refresh races not reviewed.
- `apps/users/adapters.py` `pre_social_login` (allauth adapter) — interaction with the
  `reconcile_existing_user_on_login` signal (ordering, double-fire) not traced.
- The **frontend** half of the seam: workspace switcher, connections page, whether the UI
  ever surfaces cross-tenant data boundaries or relies on backend filtering. Not opened.
- `apps/workspaces/tasks.py` janitors (`expire_inactive_schemas`,
  `expire_stale_thread_jobs`) and sibling view-schema rebuild — only referenced, not audited
  for membership/tenancy interactions.
- `apps/workspaces/api/views.py` (data-dictionary + `/refresh/` route wiring) and
  `materialization_views.py` / `jobs_views.py` — whether each route attaches the role
  permission classes from `permissions.py` was NOT verified per-route (URLConf not read);
  the "roles ~unenforced" symptom seed is therefore neither confirmed nor refuted here.
- `apps/knowledge/*` and `apps/recipes/services/runner.py` — whether recipe re-runs or
  knowledge writes respect workspace/tenant scoping. Not reviewed.
- Migration `0006_tenant_connections` data-migration correctness (TenantCredential →
  TenantConnection mapping) — not read.
</content>
