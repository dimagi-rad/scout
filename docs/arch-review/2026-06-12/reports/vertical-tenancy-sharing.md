# Vertical Review: Workspaces, Tenancy, Memberships, Roles, Sharing

*Reviewer: vertical:tenancy-sharing · 2026-06-12 · HEAD 35e4230*
*Mandate: own workspaces, WorkspaceTenant, memberships, roles + enforcement, sharing
(threads, artifacts, public views). Where is scoping derived and is it consistent?*

---

## 1. The scoping model as built

**The one good invariant:** every workspace-scoped HTTP entry point derives access from a
single source — a `WorkspaceMembership` row — through one of four near-identical resolvers:

| Resolver | Used by |
|---|---|
| `resolve_workspace_drf` (`apps/workspaces/workspace_resolver.py:12`) | workspace_views, api/views, knowledge, recipes |
| `resolve_workspace` (sync, `:35`) | artifacts (sync views) |
| `aresolve_workspace` (async, `:49`) | artifacts query-data, chat thread list, jobs, materialization views |
| `_resolve_workspace_and_membership` (`apps/chat/helpers.py:88`) | chat POST, thread messages/share/viewed (adds TenantMembership check for single-tenant) |

I found **no workspace-scoped HTTP endpoint that skips membership resolution**. Thread
endpoints additionally pin `user=` + `workspace=` (ownership), and `chat_view` explicitly
rejects foreign-thread POSTs with a logged 404 (`apps/chat/views.py:121-137`) — the
post-incident cross-workspace-thread fix is present and sound.

**Where scoping diverges:** the *schema routing* layer has two implementations that
disagree. `mcp_server/context.py:load_workspace_context` (used by chat/agent/MCP) routes
single-tenant → tenant schema, multi-tenant → `WorkspaceViewSchema`. But four HTTP
surfaces still use the legacy single-tenant shim `workspace.tenant` ("first tenant",
alphabetical by `Tenant.Meta.ordering = ["canonical_name"]`):

1. `DataDictionaryView` / `TableDetailView` (`apps/workspaces/api/views.py:241,479`)
2. `RefreshSchemaView` / `RefreshStatusView` (`api/views.py:336,387`)
3. `ArtifactQueryDataView` (`apps/artifacts/views.py:795`)
4. `RecipeRunView` TTL touch (`apps/recipes/api/views.py:116`)

For single-tenant workspaces (the demo path) this is invisible. For multi-tenant
workspaces it produces silently wrong data (findings T2, T3).

## 2. Capability scorecard (% functional, demo path vs edges)

| Capability | Functional % | Notes |
|---|---|---|
| Workspace CRUD, member mgmt, tenant add/remove | ~90% | manage-gated, last-manager guards, shared-tenant requirement on add. Edges: zero-tenant workspaces creatable; archived memberships counted (T9) |
| Role model (READ / READ_WRITE / MANAGE) | ~25–30% enforced | roles stored, returned, merged correctly — but gate only 2 of ~15 mutating surfaces + transformations (T4) |
| Tenancy scoping — single-tenant workspaces | ~95% | consistent end to end |
| Tenancy scoping — multi-tenant workspaces | ~60% | chat/MCP/query correct via view schema; dictionary, live artifacts, refresh, recipe touch all wrong (T2, T3) |
| Legacy `/refresh/` | 0% — destructive | loads into old schema, then destroys it (T1) |
| Thread sharing | backend 100%, creation UI 0% | orphaned store action; public page + endpoint live (T7) |
| Recipe-run sharing | ~50% | `is_shared` decorative (T8); `is_public` settable via API only, no UI; public page live |
| Recipe public sharing | 0% — dead | `is_public` not in update serializer; `PublicRecipePage.tsx` unrouted/unimported |
| Embed/widget | live, wired | `/widget.js` → iframe `/embed/` → `EmbedPage` router; `EmbedFrameOptionsMiddleware` + `EMBED_ALLOWED_ORIGINS`; tenant-ensure verifies against Connect API |
| User merge → workspace membership reconciliation | ~95% | transactional, role-rank upgrade on conflict; edge: TenantMetadata cascade (T12) |

---

## 3. Findings

### T1 — `/refresh/` loads fresh data into the OLD schema, then destroys it
**Status: BROKEN-NOW · Impact: data-loss · Confidence: verified-by-trace · Complexity: accidental**
(replicates v1 run A's S1; independently re-traced here)

Chain:
- UI: Data Dictionary page refresh button — `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx:33-40` → `frontend/src/store/dictionarySlice.ts:197` `POST /api/workspaces/<id>/refresh/`
- Route wired: `apps/workspaces/api/urls.py:22` → `RefreshSchemaView.post` (`apps/workspaces/api/views.py:325`)
- View creates a **new** `TenantSchema` named `t_<ext>_r<hex8>` via `create_refresh_schema` (`apps/workspaces/services/schema_manager.py:176`) and defers `refresh_tenant_schema` (`api/views.py:362-365`)
- Task `refresh_tenant_schema` (`apps/workspaces/tasks.py:126`): Step 1 creates the physical `_r` schema; Step 2 calls `run_pipeline(membership, credential, pipeline_config)` (`tasks.py:173`)
- `run_pipeline` ignores the `_r` schema entirely: `mcp_server/services/materializer.py:183` `tenant_schema = SchemaManager().provision(tenant_membership.tenant)` — `provision()` resolves by `sanitize(tenant.external_id)` **without** the `_r` suffix (`schema_manager.py:66-78`) and returns the existing ACTIVE (old) schema. All data loads into the OLD schema.
- Step 3 marks the **empty** `_r` schema ACTIVE (`tasks.py:182-184`); Step 4 flips the old (now freshly-refreshed, data-bearing) schema to TEARDOWN and schedules a drop in 30 minutes (`tasks.py:186-197`); `teardown_schema` (`tasks.py:609`) drops the physical schema and flips its runs STALE.

Net effect: ~30 minutes after clicking refresh, the just-loaded data is dropped and the
workspace's ACTIVE schema is empty. Also single-tenant-shimmed (`workspace.tenant`), and a
concurrent-refresh guard checks only PROVISIONING state. The healthy path (agent
`run_materialization` → `materialize_workspace`) does not pre-create an `_r` schema and is
unaffected; the `_r` schema concept is vestigial from a pre-pipeline design.

**Reachable via:** Data Dictionary page refresh button (RW/MANAGE role required — the one
place a role gate makes this *harder* to hit).

### T2 — Live artifact queries bypass view-schema routing in multi-tenant workspaces
**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

Chain:
- Artifact sandbox JS fetches `/api/workspaces/<id>/artifacts/<id>/query-data/` (`apps/artifacts/views.py:254`)
- `ArtifactQueryDataView.get` → `tenant = await artifact.workspace.tenants.afirst()` (`views.py:795`) → `load_tenant_context(tenant.external_id)` (`views.py:800`) → `execute_query` with `search_path` = first tenant's schema
- Contrast `mcp_server/context.py:load_workspace_context:83-139`, which routes multi-tenant workspaces to the `WorkspaceViewSchema` — the schema the agent wrote the artifact's SQL against.

Because the view schema's view names match the per-tenant table names, the query
*succeeds* against the first tenant's schema and silently returns a subset (one tenant's
rows) of what the artifact displayed at creation. No error, no indicator. "First tenant" =
alphabetically first by canonical_name, so adding a tenant can silently change which
subset is shown.

**Reachable via:** opening any artifact with `source_queries` in a multi-tenant workspace
(sandbox auto-fetches).

### T3 — Data dictionary, refresh status, and recipe TTL-touch use the first-tenant shim
**Status: BROKEN-NOW (multi-tenant only) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

- `DataDictionaryView.get` (`apps/workspaces/api/views.py:241-250`) and `TableDetailView`
  (`:479,506`) resolve `workspace.tenant` → multi-tenant workspaces show only the
  alphabetically-first tenant's tables; the view schema and other tenants never appear.
- `RefreshStatusView` (`:387`) reports only the first tenant's schema state.
- `RecipeRunView` touches only the first tenant's schema TTL (`apps/recipes/api/views.py:116-122`);
  in a workspace used only via recipes, the other constituent schemas and the view schema
  never get touched and will TTL-expire (`expire_inactive_schemas`, `tasks.py:518`),
  after which `teardown_schema` marks dependent view schemas FAILED (`tasks.py:653`) —
  the workspace goes dark despite active use. (Chat-driven use is covered by
  `touch_workspace_schemas`, `workspace_service.py:74-110`, which is correct.)

**Reachable via:** Data Dictionary page in any multi-tenant workspace; recipe runs.

### T4 — Role model is ~75% unenforced; `permissions.py` is dead code; READ role can mutate nearly everything
**Status: DEBT (BROKEN-NOW relative to the declared role semantics) · Impact: security · Confidence: verified-by-trace · Complexity: accidental**

`apps/workspaces/permissions.py` (IsWorkspaceMember/ReadWrite/Manager) has **zero
imports** anywhere in the codebase — confirmed by grep over all `*.py`. Enforcement is
re-implemented inline, but only in spots:

Enforced (role actually checked):
- Workspace rename/delete, member add/role-change/remove, tenant add/remove — MANAGE
  (`workspace_views.py:302,333,390,464,503,554,600`)
- `/refresh/` — RW+ (`api/views.py:330`); table annotation PUT — non-READ (`api/views.py:500`)
- Transformations writes — RW+ (`apps/transformations/views.py:84,154`) — the only other app that checks

NOT enforced (membership resolved, role discarded — `_membership` pattern):
- Knowledge create/update/delete/import — all 7 sites (`apps/knowledge/api/views.py`)
- Recipes create/update/delete/**run**/share-toggle — all sites (`apps/recipes/api/views.py`)
- Artifacts PATCH/DELETE/undelete/export (`apps/artifacts/views.py:893,915,926,942`)
- Jobs cancel, materialization cancel/retry (`jobs_views.py:166`, `materialization_views.py:42,145`)
- **Chat** (`apps/chat/views.py`): any member streams the agent, which carries
  `create_artifact`/`update_artifact`/`save_learning`/`save_recipe` (platform-DB writers,
  `graph/base.py:692-695`) and the MCP tools `run_materialization` and
  `teardown_schema`. `chat_view` hard-codes `"user_role": "analyst"`
  (`chat/views.py:214`) — the workspace role never reaches the agent or MCP.

Net: a READ-role member is read-only on exactly two HTTP endpoints, while being able to
run/cancel materializations, drop all workspace schemas via the agent (T5), create/delete
knowledge, recipes, artifacts, and toggle public sharing of runs. The role column is
faithfully stored, returned, displayed, and merged (`merge.py:_ROLE_RANK`) — it just
doesn't gate much. Essential vs accidental: the enforcement *gap* is accidental
(inline-check pattern never finished); having roles at all is essential product surface.

### T5 — Agent `teardown_schema` drops tenant schemas shared with other workspaces, with no role gate
**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: mixed**

Chain: chat → agent (any member, any role) → MCP `teardown_schema(confirm=True)`
(`mcp_server/server.py:802`) → drops the workspace view schema and **every TenantSchema
of every tenant in the workspace** (`server.py:851-858`). Tenant schemas are keyed by
provider external_id alone (`schema_manager.py:66`) and shared across all workspaces
containing that tenant. The docstring says "Drop all materialized data for this
workspace" — actually it drops data for every sibling workspace too. Post-#230,
`_fail_dependent_view_schemas` at least flips sibling view schemas to FAILED honestly,
and data is re-materializable, so impact is disruption not permanent loss. The only
guards are `confirm=True` (LLM-suppliable) and workspace existence — no role, no
ownership, no "other workspaces depend on this" check.

**Reachable via:** any chat message that persuades the agent ("reset my data").

### T6 — `cancel_materialization` / `get_materialization_status` MCP tools are unscoped: LLM-supplied run_id, no workspace/membership check
**Status: LATENT · Impact: security · Confidence: verified-by-trace · Complexity: accidental**

These two tools take a bare `run_id` (`mcp_server/server.py:408,446`) and are **not** in
`MCP_TOOL_NAMES` (`apps/agents/graph/base.py:65-76`), so:
- their schemas pass to the LLM unmodified (`base.py:408-409` — `result.append(tool); continue`),
- the injecting node does not override their args (`base.py:460` injects only for `MCP_TOOL_NAMES`),
- all MCP tools are bound (`_build_tools`, `base.py:692` `tools = list(mcp_tools)`).

So a user can tell their agent "check/cancel materialization run `<uuid>`" for a run
belonging to **any tenant in any workspace**. `get_materialization_status` leaks the
target's `tenant_id` (external id) and physical `schema_name` (`server.py:426-438`);
`cancel_materialization` flips any in-flight run to FAILED (`server.py:478-482`).
Severity is limited by run-id secrecy (UUID4) and the MCP server being internal-only
(docker `expose`, DNS-rebinding allowlist `server.py:909-912`), but run_ids do circulate
(jobs API responses, agent transcripts, shared threads — a publicly shared thread's tool
output can contain a run_id). Contrast `run_materialization`, which does check the user
has a tenant membership in the workspace (`server.py:553-555`).

### T7 — Share surface drift: live public endpoints + tokens with no creation UI, dead public-recipe surface
**Status: DEBT · Impact: security (hygiene) / velocity · Confidence: verified-by-trace · Complexity: accidental**

- **Threads:** `PATCH /api/workspaces/<id>/threads/<id>/share/` is live
  (`apps/chat/thread_views.py:163`), and the store action exists
  (`frontend/src/store/uiSlice.ts:78-90`), but **no component calls it** (grep: zero
  callers outside uiSlice). The public endpoint (`/api/chat/threads/shared/<token>/`,
  no auth) and `/shared/threads/<token>` page (`App.tsx:21-22`) remain fully live, and
  serve messages **plus every artifact's full code and data**
  (`thread_views.py:52-68,227-257`). Tokens minted before the 2026-06-04 UI removal
  remain valid indefinitely — there is no revocation sweep and no owner-visible list of
  what is still shared.
- **Recipe runs:** `RecipeRunUpdateSerializer` accepts `is_public`
  (`apps/recipes/api/serializers.py:113`) and the public endpoint + `/shared/runs/<token>`
  page are live, but the UI exposes only the `is_shared` checkbox
  (`RecipeRunDetail.tsx:207`) — public sharing is API-only.
- **Recipes:** `Recipe.is_public`/`share_token` exist (`models.py:71-82`) but
  `RecipeUpdateSerializer` excludes them (`serializers.py:76`), there is no public recipe
  endpoint, and `PublicRecipePage.tsx` has no route and no importer — dead model fields +
  dead 200-line component.
- **Not drift:** `/widget.js` + `/embed` are intentional and wired
  (`config/views.py:6`, `frontend/src/App.tsx:44-45`, `EmbedPage.tsx:20-50`,
  `config/middleware/embed.py`, `EMBED_ALLOWED_ORIGINS` gating SameSite=None in
  `production.py:23-25`); embed tenant-ensure verifies against the Connect API before
  minting memberships (`apps/users/views.py:382-398`).

### T8 — `Recipe.is_shared` is decorative: "private" recipes are visible and runnable by all members
**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

Model help text: "If true, all project members can view and run this recipe"
(`apps/recipes/models.py:67-70`, default False); the UI checkbox repeats the promise
(`RecipeDetail.tsx` "Share with project … All project members can view and run this
recipe"). But every recipe view filters only by workspace:
`Recipe.objects.filter(workspace=workspace)` (`apps/recipes/api/views.py:38,55,94,137,154`)
— never by `is_shared` or `created_by`. Same for `RecipeRun.is_shared`
(`models.py:348-351`, "Visible to all project members"): `RecipeRunListView` returns all
runs (`views.py:140`). The unshared default is a privacy expectation the backend never
implements. (The `["workspace", "is_shared"]` index, `models.py:109`, indexes a column no
query uses.)

### T9 — `archived_at` on TenantMembership is enforced inconsistently (5+ authz sites count archived memberships)
**Status: LATENT · Impact: correctness/security · Confidence: verified-by-trace · Complexity: accidental**

Archive-on-disconnect (connection removal archives memberships,
`apps/users/views.py:285-286`, `auth_views.py:204`) is respected by: tenant listing
(`auth_views.py:75,123`), connection views, and `materialize_workspace`
(`tasks.py:231-232`). It is **ignored** by:
- chat's single-tenant gate (`apps/chat/helpers.py:117` — archived membership still grants chat)
- member-add shared-tenant check (`workspace_views.py:411-414`)
- workspace-create tenant validation (`workspace_views.py:173-178`) and tenant-add (`:573`)
- MCP `run_materialization` guard (`mcp_server/server.py:509-515` — passes the guard,
  then the task filters archived and fails with a different error)
- `/refresh/` membership lookup (`api/views.py:343`)

So disconnecting a credential revokes visibility in the connections UI but not workspace
access paths. Two behaviors for one flag; either direction may be intended, but not both.

### T10 — Tenant-membership requirement is asymmetric between single- and multi-tenant workspaces
**Status: LATENT (design) · Impact: security · Confidence: verified-by-trace (mechanism), hypothesis (intent) · Complexity: essential (needs a documented decision)**

`_resolve_workspace_and_membership` (`apps/chat/helpers.py:106-108`): multi-tenant →
WorkspaceMembership alone suffices; single-tenant → TenantMembership also required
(`chat/views.py:113-114` rejects otherwise). Combined with member-add requiring a shared
membership in **any one** workspace tenant (`workspace_views.py:411-414`), a user who
belongs to tenant A alone gets full SQL access to tenants B and C's rows in an A+B+C
workspace (view schema queries run as the platform readonly role, not user credentials).
Plausibly intended ("workspace = sharing boundary"), but it makes the TenantMembership
check in the single-tenant path security theater — the same user reading the same data
needs a tenant credential in one topology and not the other.

### T11 — Member removal deletes the member's threads, but LangGraph checkpoints are never deleted
**Status: LATENT · Impact: correctness (privacy/storage) · Confidence: strong-inference · Complexity: accidental**

`WorkspaceMemberDetailView.delete` deletes the removed member's Thread rows
(`workspace_views.py:517`); workspace deletion cascades all threads. Nothing anywhere in
`apps/` deletes the corresponding LangGraph checkpoints (grep for thread/checkpoint
deletion: zero hits) — conversation content (including query results) persists in the
platform DB indefinitely after removal. Side quirk: `chat_view`'s ownership check passes
for a deleted thread id (row gone), so a re-added member POSTing the old thread UUID gets
a fresh Thread row whose checkpointer silently resumes the old conversation
(`views.py:122-126`, `_upsert_thread`). Also inconsistent ownership semantics: threads
are destroyed on removal, while the member's artifacts/recipes/knowledge survive.

### T12 — Merge: tenant-membership conflict deletion cascades TenantMetadata
**Status: LATENT · Impact: correctness · Confidence: strong-inference · Complexity: accidental**

`_merge_tenant_memberships` deletes the duplicate's membership when canonical already has
that tenant (`apps/users/services/merge.py:166`). `TenantMetadata` is a OneToOne on
TenantMembership with CASCADE (`apps/workspaces/models.py:266-270`), so discovered
provider metadata riding on the duplicate's membership is destroyed even when the
canonical's membership has none (re-discovery repairs it on next materialization, but
"survives schema teardown so re-provisioning can skip re-discovery" — the model's own
purpose — is silently defeated). Similarly, the surviving membership keeps
`connection=None` if only the duplicate's membership was wired to a connection whose row
was conflict-merged. Low frequency (merge events are rare), transactional, no data-loss
beyond metadata.

### T13 — Minor inventory
- `WorkspaceListView.post` permits zero-tenant workspaces (`tenant_ids` defaults `[]`,
  `workspace_views.py:170`); downstream degrades to 503s/403s rather than breaking, but
  nothing prevents or cleans them. COSMETIC.
- `materialization_cancel_view` cancels "orphan" runs scoped by **tenant**
  (`materialization_views.py:48-52` `tenant_schema__tenant__in=workspace.tenants`), so a
  member of workspace A can cancel an untracked `/refresh/` run started by a user of
  sibling workspace B sharing the tenant. The tracked-job path correctly restricts to
  `thread__user=user` with an explicit comment. LATENT/correctness, small.
- Artifacts docstrings still say "project membership" / "UUID of the TenantMembership"
  (`artifacts/views.py:725,950`) — projects→workspaces rename residue; the code checks
  workspace membership. COSMETIC.
- PNG/PDF artifact export returns 501 by design (`artifacts/views.py:980-986`) — a
  capability advertised by the route but not functional (artifacts vertical's scope).

---

## 4. What's actually fine (verified)

- **Uniform membership-based scoping** at every workspace HTTP entry point (4 resolver
  variants, all keyed on the same WorkspaceMembership row; no bypasses found).
- **Thread ownership + cross-workspace rejection** in chat (`chat/views.py:121-137`,
  `thread_views.py:146-156` "haunted chat" 404) — the 2026-06-10 threadId fix is in and
  defensible.
- **Post-incident TTL fixes present**: provision resurrect resets `last_accessed_at`
  (`schema_manager.py:114-122`); teardown flips dependent sibling view schemas to FAILED
  (`tasks.py:647-653`); `touch_workspace_schemas` bulk-touches constituent tenant schemas
  for multi-tenant chat (`workspace_service.py:96-110`).
- **Share token mechanics**: `secrets.token_urlsafe(32)`, unique+indexed, regenerated on
  re-enable, nulled on disable (`chat/models.py:41-48`, `recipes/models.py:117-120,388-391`);
  public views correctly require `is_shared=True`/`is_public=True`.
- **Manager safety rails**: last-manager demote/remove guards (`workspace_views.py:26-30,477-486,510-514`);
  member-add restricted to users sharing a workspace tenant; tenant-add requires the
  requester's own TenantMembership; last-tenant removal blocked with row locking
  (`workspace_service.py:48-57`).
- **Jobs/cancel scoping**: ThreadJob endpoints pin workspace + thread owner
  (`jobs_views.py:176-183`); tracked-job cancellation can't touch other users' jobs.
- **merge_users**: single transaction, explicit conflict handling per relation,
  role-rank upgrade, long-tail FK sweep with documented IntegrityError tripwire.
- **MCP exposure posture**: internal-only (compose `expose`, not `ports`; DNS-rebinding
  allowlist), oauth tokens via `_meta` never LLM-visible, `workspace_id` injected
  server-side and hidden from the LLM schema for context-scoped tools.
- **Embed plumbing**: dedicated frame-options middleware, SameSite=None only when
  `EMBED_ALLOWED_ORIGINS` set, tenant-ensure verified against the Connect API.
- **Transformations app** is the existence proof that role enforcement was intended:
  it checks RW on every write (`transformations/views.py:74-87,152-157`).

## 5. Cross-cutting observations

1. **Two scoping regimes, one renamed concept.** Membership scoping (who may call) is
   centralized and consistent; schema scoping (what data the call touches) is split
   between the modern `load_workspace_context` and four legacy `workspace.tenant` shims.
   Every multi-tenant bug in this report (T1–T3) is a shim site. The shims are explicitly
   labeled "compatibility" in `models.py:143-166` — the migration was never finished.
2. **Roles were built as data, never as policy.** Model, API, UI, merge logic all handle
   roles correctly; only the gate layer is missing (permissions.py written then orphaned;
   inline checks added only where someone hit a need). The agent path makes this worse:
   role-blind chat grants every member the most destructive operations in the system.
3. **Sharing is a one-way ratchet right now**: creation UI removed, revocation UI also
   gone, tokens immortal, no audit view. Either re-add UI or sweep `is_shared=True` rows.
4. **The tenant-schema-shared-across-workspaces model** (provision keyed on external_id)
   is essential complexity — it's what makes multi-workspace/multi-user tenants cheap —
   but three different surfaces (agent teardown T5, TTL touch T3, orphan-run cancel T13)
   reason about it as if schemas were workspace-private.

## 6. Coverage log

**Deep-read (line-by-line):**
`apps/workspaces/models.py`, `permissions.py`, `workspace_resolver.py`,
`api/workspace_views.py`, `api/views.py`, `api/jobs_views.py` (85-190), `api/jobs_cancel.py`,
`api/materialization_views.py` (30-75), `services/workspace_service.py`,
`services/schema_manager.py` (33-190), `tasks.py` (60-200, 500-665),
`apps/chat/views.py`, `thread_views.py`, `helpers.py`, `models.py`,
`apps/recipes/api/views.py`, `models.py` (share fields), `api/serializers.py` (field lists),
`apps/artifacts/views.py` (670-990),
`apps/users/models.py`, `signals.py`, `services/merge.py`, `services/credential_resolver.py`,
`views.py` (340-420),
`mcp_server/context.py`, `auth.py`, `server.py` (407-520, 800-915),
`apps/agents/graph/base.py` (55-200, 395-535, 668-740),
`config/views.py`, `config/settings/production.py`, `config/urls.py` (workspace section),
`apps/workspaces/api/urls.py`,
`frontend/src/App.tsx`, `pages/EmbedPage.tsx`, `pages/DataDictionaryPage/DataDictionaryPage.tsx` (1-80),
`router.tsx` (route list).

**Skimmed (targeted greps + excerpts):**
`apps/workspaces/tasks.py` (rest), `mcp_server/services/materializer.py` (run_pipeline →
provision call only), `apps/users/auth_views.py`, `tenant_resolution.py` (archived_at
sites), `apps/knowledge/api/views.py` (resolver/role sites), `apps/transformations/views.py`
(role checks), `frontend/public/widget.js` (head), `frontend/src/store/uiSlice.ts`,
`dictionarySlice.ts`, `pages/RecipesPage/{RecipeDetail,RecipeRunDetail}.tsx` (sharing
sections), `hooks/useWorkspaceThreadSync.ts` (top), `config/settings/base.py` (DRF/middleware),
`docker-compose.yml`, `config/deploy*.yml` (MCP exposure), `apps/recipes/admin.py`.

**Not examined (honest gaps for the gap loop):**
- `apps/recipes/services/runner.py` — how RecipeRunner scopes the re-invoked agent
  (workspace? user oauth? sync-in-DRF concerns) — recipes vertical, but the run path is
  a sharing-adjacent mutation I did not trace.
- `apps/artifacts/services/export.py` and the sandbox CSP/renderer internals.
- `mcp_server/services/query.py` SET ROLE mechanics and `sql_validator.py` (whether the
  readonly role actually prevents cross-schema reads — relevant to T10's severity).
- `apps/agents/tools/*` internals (artifact/recipe/learning tool argument validation).
- `apps/users/services/tenant_resolution.py` full logic, `ocs_team.py`, adapters,
  token_refresh — accounts vertical.
- `apps/workspaces/tasks.py` 200-510 (materialize_workspace body, resume task, janitors)
  beyond the excerpts quoted — materialization vertical.
- `schema_manager.py` 190-660 (view-schema build SQL, truncation-safe names — incident
  fix #227 not independently re-verified here).
- Frontend `WorkspaceDetailPage` (1,045 LOC: members/tenants UI wiring vs API), the full
  `useWorkspaceThreadSync` reconciliation, `WorkspaceSwitcher`.
- Tests: I did not audit which of these gaps tests would have caught
  (`tests/test_workspace_*`, `test_thread_share*` etc. unopened).
- Admin surface (`admin.py` registrations beyond recipes) as an unaudited write path to
  share fields.
- Migration history for WorkspaceMembership/share fields.
