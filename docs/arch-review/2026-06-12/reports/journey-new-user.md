# Journey Tracer Report — New User Lifecycle

*Reviewer: journey tracer (new-user). Mandate: follow the user, not the module.
Report only; no code changed. Evidence standards per `docs/arch-review-methodology.md`.*

This report traces complete user journeys from signup through chat, materialization,
artifacts, sharing, roles, and the legacy refresh path — following the flow across
`users` → `workspaces` → `agents` → `chat` → MCP → worker → `artifacts`, and stopping
at every point where one subsystem's assumption breaks in another.

Confidence labels: `verified-by-trace` (I followed the quoted chain), `strong-inference`
(logic verified by reading, runtime not executed), `hypothesis` (plausible, unverified).

---

## Journeys traced

1. **J1 — OAuth signup → tenant resolution → workspace auto-create.**
   allauth callback → `resolve_tenant_on_social_login` (`apps/users/signals.py:55`) →
   `resolve_{commcare,connect,ocs}_*` (`tenant_resolution.py`) → `TenantMembership`
   `aget_or_create` → `post_save` → `auto_create_workspace_on_membership`
   (`signals.py:21`) creates `Workspace` + `WorkspaceTenant` + `WorkspaceMembership(MANAGE)`.
2. **J2 — First chat turn → materialization → resume.**
   `POST /api/chat/` (`chat/views.py:65`) → `_resolve_workspace_and_membership` →
   `_build_system_prompt` injects schema state → agent calls MCP `run_materialization`
   → `materialize_workspace` worker task → `resume_thread_after_materialization`.
3. **J3 — Ask a question → live artifact → render/export.**
   agent `create_artifact` tool stores `source_queries` → `ArtifactSandboxView` /
   `ArtifactQueryDataView` execute live SQL at view time.
4. **J4 — Share a thread → second user opens it.**
   `thread_share_view` PATCH sets `is_shared` → `public_thread_view` (no auth) /
   frontend `/shared/threads/<token>`.
5. **J5 — Multi-tenant workspace** (add a 2nd tenant) → view-schema build → chat,
   data dictionary, artifacts against the `ws_*` view schema.
6. **J6 — Legacy data refresh** — Data Dictionary "refresh" button →
   `RefreshSchemaView` → `refresh_tenant_schema` worker task.

---

## Findings

### F1 — Legacy `/refresh/` loads into the canonical schema but activates an empty one, then schedules the data schema for teardown  [BROKEN-NOW / data-loss]

**Confidence: strong-inference** (full code chain read; runtime not executed).
**Reachable via:** Data Dictionary "refresh" button (`refresh-schema-btn`), single-tenant workspaces.

The refresh task and the pipeline disagree about *which schema* receives the data.

Chain:
- `RefreshSchemaView.post` creates a new schema row with a **unique** name and dispatches the task:
  `apps/workspaces/api/views.py:362` → `SchemaManager().create_refresh_schema(tenant)`.
- `create_refresh_schema` builds a *distinct* schema name: `schema_manager.py:176`
  `schema_name = f"{self._sanitize_schema_name(tenant.external_id)}_r{uuid.uuid4().hex[:8]}"`.
- The task creates that physical `_r<hex>` schema, then runs the pipeline:
  `tasks.py:150` (`create_physical_schema`) and `tasks.py:173`
  `await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)`.
- But `run_pipeline` **ignores** the `_r<hex>` schema and provisions the *canonical*
  schema from `external_id`: `mcp_server/services/materializer.py:183`
  `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`, and
  `provision` computes `schema_name = self._sanitize_schema_name(tenant.external_id)`
  (`schema_manager.py:66`) — the canonical `t_<id>` name, not the `_r<hex>` name. All
  rows load into the canonical schema (`materializer.py:184` `schema_name = tenant_schema.schema_name`).
- The task then marks the **empty** `_r<hex>` schema ACTIVE: `tasks.py:182-184`.
- Step 4 tears down every *other* ACTIVE schema for the tenant, excluding the `_r` row:
  `tasks.py:188-197` filters `state=ACTIVE` `.exclude(id=new_schema.id)` → the
  **canonical, data-bearing** schema is flipped to TEARDOWN and `teardown_schema`
  is deferred (30-min delay). `teardown_schema` then `DROP SCHEMA ... CASCADE` the
  canonical schema and marks its runs STALE (`tasks.py:610-645`).

Consequence: immediately after a refresh, two schemas are ACTIVE for the tenant — the
canonical one (with data) and the `_r<hex>` one (empty). `load_tenant_context` resolves
by `tenant__external_id` + `state in (ACTIVE, MATERIALIZING)` `.afirst()`
(`mcp_server/context.py:56-59`) with no deterministic tie-break, so routing is
ambiguous; and after the 30-minute teardown only the **empty** `_r<hex>` schema
survives. The agent/data dictionary then query an empty schema.

This corroborates v1 run A's S1 ("refresh loads data then destroys it") with a precise
mechanism. The `SynchronousOnlyOperation` fix in this task (`8104ce1`, per cartography)
addressed the async resolver, not this schema-targeting defect.
**Essential vs accidental:** accidental — two schema-naming conventions (`provision`'s
canonical name vs `create_refresh_schema`'s `_r<hex>`) that were never reconciled.

---

### F2 — Multi-tenant artifact live queries route to the first tenant's schema, where the namespaced views don't exist  [BROKEN-NOW / correctness]

**Confidence: verified-by-trace.**
**Reachable via:** any artifact with `source_queries` created in a multi-tenant workspace, opened/refreshed in the UI.

In a multi-tenant workspace the agent is told tables are namespaced views
`{tenant_name}__{table_name}` living in the `ws_*` view schema:
`apps/agents/graph/base.py:299-303` (`_MULTI_TENANT_NAMESPACE_HINT`). The agent therefore
writes artifact `source_queries` referencing `prefix__table` names.

But the live-query endpoint resolves the workspace's **first tenant** and routes to that
tenant's single-tenant schema, not the workspace view schema:
- `apps/artifacts/views.py:795` `tenant = await artifact.workspace.tenants.afirst()`
- `:800` `ctx = await load_tenant_context(tenant.external_id)` → resolves the per-tenant
  `t_<id>` schema (`mcp_server/context.py:47-80`), whose `search_path` is
  `t_<id>,public` (`context.py:159`).

The `prefix__table` views only exist in the `ws_*` view schema
(`schema_manager.py:367-375`), never in `t_<id>`. So every live query in a multi-tenant
artifact fails table-not-found (or, worse, silently resolves a same-named table in the
wrong tenant if one happens to exist). Contrast `chat`/MCP, which correctly route via
`load_workspace_context` (`mcp_server/context.py:83`) that picks the view schema for
multi-tenant. The artifact path uses `load_tenant_context` directly and never branches on
tenant count.
**Essential vs accidental:** accidental — `ArtifactQueryDataView` predates the
multi-tenant view-schema routing and was not migrated to `load_workspace_context`.

---

### F3 — Data Dictionary and `/refresh/` are single-tenant-only; multi-tenant workspaces silently see only their first tenant  [BROKEN-NOW / correctness]

**Confidence: verified-by-trace.**
**Reachable via:** Data Dictionary page and refresh button on any multi-tenant workspace.

`DataDictionaryView`, `TableDetailView`, and `RefreshSchemaView` all operate on
`workspace.tenant` — a compatibility shim that returns `self.tenants.first()`
(`apps/workspaces/models.py:144-146`):
- `apps/workspaces/api/views.py:241` `_schema_unavailable_response(workspace.tenant)`
- `:245` `_resolve_tenant_schema(workspace.tenant)`
- `:336` (refresh) `tenant = workspace.tenant`; `:387` (status) `tenant = workspace.tenant`.

For a multi-tenant workspace this silently picks one arbitrary tenant: the data
dictionary lists only that tenant's tables (never the namespaced `ws_*` views), and the
refresh button refreshes only one tenant — while the chat/MCP layer correctly serves the
unioned view schema. The two surfaces present divergent table universes for the same
workspace. (`RefreshSchemaView` also feeds F1, compounding the damage on multi-tenant.)
**Essential vs accidental:** accidental — these DRF views were never updated when
multi-tenant view schemas were introduced.

---

### F4 — `WorkspaceRole.READ` is unenforced on the primary write surfaces (chat, knowledge, recipe runs, artifacts)  [BROKEN-NOW / correctness, security-adjacent]

**Confidence: verified-by-trace.**
**Reachable via:** any user added to a workspace with role `read` who then chats, runs a recipe, or edits knowledge.

`WorkspaceRole` defines `read / read_write / manage` (`models.py:103-106`). Role is
enforced only on a subset of endpoints (workspace rename/delete, member/tenant
management in `workspace_views.py`; `/refresh/` and `TableDetailView.put` in
`api/views.py:330,500`). The DRF permission classes that would enforce it everywhere
are dead code:
- `apps/workspaces/permissions.py` defines `IsWorkspaceMember/ReadWrite/Manager`, but a
  repo-wide grep finds **zero** importers outside the file itself.

The high-value write paths gate on *membership only*, not role:
- Chat: `chat_view` calls `_resolve_workspace_and_membership` (any role) and then
  hardcodes `"user_role": "analyst"` (`chat/views.py:213`). A `read` member can drive the
  agent, which can `create_artifact`, `save_learning`, `save_as_recipe`, `run_materialization`,
  and `teardown_schema`.
- Knowledge API: `permission_classes = [IsAuthenticated]` only (`knowledge/api/views.py:46,163,234,268`).
- Recipe run path re-invokes the agent with `"user_role": "analyst"`
  (`recipes/services/runner.py:223,310`) — no role check.
- Artifacts: `ArtifactDetailView` / `ArtifactQueryDataView` gate on `resolve_workspace`
  membership only (`artifacts/views.py:887,778`).

So `read` is effectively `read_write` everywhere that matters. This matches both v1 runs'
"roles ~unenforced" observation; it remains live. The hardcoded `user_role: "analyst"`
also means the agent's own state never learns the caller's role.
**Essential vs accidental:** accidental — a permission layer was designed
(`permissions.py`) but never wired in.

---

### F5 — Auto-merge after OAuth never fires for these providers because no verified `EmailAddress` is ever created  [LATENT / correctness]

**Confidence: strong-inference.**
**Reachable via:** a user who logs in with two providers that both report the same email.

`reconcile_existing_user_on_login` refuses to merge unless the canonical user owns a
*verified* `EmailAddress` row (`signals.py:104-115`):
```
canonical_owns_email = EmailAddress.objects.filter(
    user=canonical, email__iexact=new_email, verified=True).exists()
if not canonical_owns_email:  # → logs "Refusing auto-merge" and returns
```
Nothing in the OAuth flow creates a verified `EmailAddress` for the custom CommCare /
Connect / OCS providers — `resolve_tenant_on_social_login` and `tenant_resolution`
only create `Tenant`/`TenantMembership`/`Workspace`, never `EmailAddress`. The refusal is
the documented secure-by-design behavior (project memory: "refusal is by-design"), so
this is **not** a new defect — but the gate condition is a near-constant `False`, so the
auto-merge branch is effectively dead and duplicate `User` rows (same email, different
provider) accumulate and must be merged by an operator. Worth surfacing as latent debt,
not an active break.
**Essential vs accidental:** essential (security posture) but the dead-branch shape is
accidental.

---

### F6 — `run_materialization` permits two chats in the same workspace to launch parallel materializations of shared tenant schemas; ThreadJob is created after dispatch (race)  [LATENT / correctness]

**Confidence: verified-by-trace** (the code documents both gaps explicitly).
**Reachable via:** two chat threads in one workspace (or two workspaces sharing a tenant) materializing concurrently.

The in-flight guard is scoped by `thread_id`, not by tenant/workspace, and the comment
acknowledges the consequence: `mcp_server/server.py:572-590` — "this lets two threads in
the same workspace dispatch parallel materializations that share tenant_schemas ... the
materializer has no advisory lock per tenant_schema." Each `run_pipeline` `provision()`s
and writes the same `t_<id>` tables; concurrent loaders against the same physical schema
can interleave DROP/CREATE/INSERT.

Separately, the ThreadJob row is created *after* `defer_async` returns
(`server.py:606-635`), so the worker can finish before the row is visible; the resume
deferral hedges with a bounded backoff (`tasks.py:363-396`) and `tasks.py:373` carries
the standing TODO: "a cleaner fix is to let MCP write a placeholder ThreadJob *before*
defer_async." The janitor (`expire_stale_thread_jobs`) is the backstop. These are known,
mitigated races — recording them as latent because the mitigations are timing-based, not
structural.
**Essential vs accidental:** accidental — missing per-tenant lock + create-order coupling
to procrastinate's id.

---

### F7 — A `read`-role / second user who opens a shared thread reaches a live-query surface scoped only by workspace membership, not by share intent  [LATENT / security-adjacent]

**Confidence: strong-inference.**
**Reachable via:** public shared-thread page; second workspace member opening a thread's artifacts.

`public_thread_view` (`chat/thread_views.py:227`) is unauthenticated and returns the
thread's messages **and** its artifacts' `code`/`data` (`_get_thread_artifacts`,
`:52-68`) — but it returns the artifact's stored static `data`, not a live-query route, so
the public page itself does not execute SQL. However, the artifact **live-query** endpoint
(`ArtifactQueryDataView`) authorizes on workspace membership only (`artifacts/views.py:778`),
with no per-artifact share scoping and no role gate (see F4). Combined with F4, any
workspace member — including `read` — can pull live tenant data for any artifact in the
workspace by id. The public share surface is read-static and bounded; the authenticated
live-query surface is the looser one. The share-creation UI was removed 2026-06-04 but
`public_thread_view`, `share_token`, and the `/shared/threads/<token>` frontend route
remain live (`App.tsx:22`), so old tokens still resolve.
**Essential vs accidental:** accidental — share surface drift; authz scoped to workspace
rather than to the shared resource.

---

## Cross-subsystem assumption breaks (summary)

| Where assumed | Where it breaks | Finding |
|---|---|---|
| Pipeline provisions schema from `external_id` | Refresh task created a different `_r<hex>` schema | F1 |
| Multi-tenant tables are `ws_*` namespaced views | Artifact live-query routes to `t_<id>` schema | F2 |
| Workspace may have ≥2 tenants | DataDictionary/Refresh use `workspace.tenant` (first only) | F3 |
| Roles gate writes | Only membership is checked on chat/knowledge/recipes/artifacts | F4 |
| Verified email enables merge | No `EmailAddress` ever created for these providers | F5 |
| One materialization per tenant schema | Guard is per-thread; no tenant lock | F6 |
| Sharing scopes access | Live-query authz is workspace-wide | F7 |

---

## What's fine (verified healthy in this journey)

- **Cross-workspace `threadId` bleed is fixed.** `setActiveDomain` resets to a fresh
  `crypto.randomUUID()` on workspace switch (`store/domainSlice.ts:53-70`); chat POST and
  thread-messages views reject foreign threads with 404 (`chat/views.py:125-137`;
  `thread_views.py:146-156`). The 2026-06-10 symptom (c) is closed.
- **63-byte view-name truncation collisions are guarded.** `_view_prefix` caps prefixes to
  32 chars with a deterministic digest, and `build_view_schema` hard-fails on oversized or
  colliding final names *before* any DDL (`schema_manager.py:219-350`). Symptom (1a) closed.
- **TTL-vs-provision resurrection is fixed.** `provision` sets `last_accessed_at = now()`
  when activating/resurrecting (`schema_manager.py:120-122`), and `touch_workspace_schemas`
  bulk-touches constituent tenant schemas for multi-tenant chat
  (`workspace_service.py:74-110`). Symptom (1b) closed.
- **View-schema build failures surface instead of being swallowed.** `materialize_workspace`
  records `state=FAILED`/`last_error` and the resume task tells the agent "do NOT re-run
  materialization" (`tasks.py:322-336, 1073-1095`); `get_schema_status` reports `failed`
  (`server.py:743-764`). Symptom (1d) closed.
- **OAuth tokens are encrypted at rest** via the Fernet adapter (`adapters.py:54-72`).
- **Thread-ownership checks return 404 (not 403)** to avoid existence leaks
  (`chat/views.py:118`).
- **OCS team fail-closed guard** prevents reusing a token scoped to a different team
  (`credential_resolver.py:53-64`).
- **Schema-name interpolation is defended** by a `^[a-z][a-z0-9_]*$` re-check before
  building the psycopg `options` string (`context.py:147-159`).

---

## Coverage log (honest)

**Deep-read (line-by-line):**
- `apps/users/signals.py`, `apps/users/adapters.py`, `apps/users/services/tenant_resolution.py`,
  `apps/users/services/credential_resolver.py`, `apps/users/views.py`, `apps/users/models.py`
- `apps/workspaces/services/workspace_service.py`, `apps/workspaces/workspace_resolver.py`,
  `apps/workspaces/permissions.py`, `apps/workspaces/api/workspace_views.py`,
  `apps/workspaces/api/views.py`, `apps/workspaces/services/schema_manager.py`
- `apps/workspaces/tasks.py` (full)
- `apps/chat/views.py`, `apps/chat/helpers.py`, `apps/chat/thread_views.py`, `apps/chat/models.py`
- `apps/agents/graph/base.py`, `apps/agents/mcp_client.py`
- `mcp_server/server.py`, `mcp_server/context.py`, `mcp_server/auth.py`
- `mcp_server/services/materializer.py` lines ~80-320, plus `provision`/load-target greps
- `apps/artifacts/views.py` lines 660-989
- `frontend/src/router.tsx`, `frontend/src/App.tsx`, `frontend/src/hooks/useWorkspaceThreadSync.ts`,
  `frontend/src/store/domainSlice.ts`, `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx`

**Skimmed (read partially / via grep):**
- `apps/agents/tools/artifact_tool.py` (signatures + persistence only)
- `apps/recipes/services/runner.py` (role + reinvoke lines), `apps/recipes/api/views.py` (perms)
- `apps/knowledge/api/views.py` (permission classes only)
- `apps/workspaces/api/jobs_views.py` (outline only)
- `apps/users/services/token_refresh.py`, `apps/users/services/ocs_team.py` (referenced, not opened)
- `frontend/src/pages/PublicThreadPage.tsx` (routing presence only)

**NOT examined (gaps for later phases):**
- `apps/agents/memory/checkpointer.py`, `apps/chat/stream.py`, `apps/chat/message_converter.py`,
  `apps/chat/rate_limiting.py` — the SSE/streaming + checkpoint serialization path was not traced.
- `mcp_server/services/sql_validator.py`, `services/query.py`, `services/metadata.py` internals
  (the SQL allow-list and `SET ROLE` enforcement were not independently verified).
- `mcp_server/loaders/*` (19 files) — provider loader integrity (Connect row-duplication
  siblings) is owned by another reviewer; not opened here.
- `apps/transformations/*` — `get_lineage`/dbt path touched only via the prompt hint.
- `apps/users/services/merge.py` body — only the *gate* in `signals.py` was read, not the
  merge mechanics themselves.
- `apps/artifacts/services/export.py` — PDF/PNG export path not traced (export view returns
  501 for png/pdf, but the async exporter was not opened).
- `apps/users/auth_views.py` (login/signup/csrf), `apps/users/decorators.py`,
  `config/urls.py` route wiring — assumed correct, not verified.
- `frontend/src/pages/WorkspaceDetailPage/` (1,045 LOC) — the materialization-controls and
  members UI were not opened; only the data-dictionary page and switcher/sync were.
- Runtime verification: **nothing was executed.** F1's data-loss consequence and F5's
  dead-branch claim are strong inference from reading, not observed.
