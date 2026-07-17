# Architecture Review — Generalist 1

*Role: full-codebase generalist pass (fresh eyes, no cartography). Evidence standards per `docs/arch-review-methodology.md`. Report only; no code changed.*

---

## 1. Executive summary

Scout's core query path (chat → agent graph → MCP `query` tool → sqlglot validation → read-only role) is genuinely well built, and the recent incident-response work (PRs #227–#232) shows careful, CAS-disciplined state-machine engineering. But the system around that core has the classic shape of fast, deep, AI-generated development with no wide passes:

1. **The same scoping question — "which schema does this workspace query?" — is answered at least five different ways** (`load_workspace_context`, `load_tenant_context`, `workspace.tenant` shim, `tenants.afirst()`, raw `schema_name` lookups). Three of those ways are wrong for multi-tenant workspaces, and one of them (the refresh path) destroys data.

2. **Features evolve in one place and their siblings are left behind.** `materialize_workspace` got sibling-view-schema rebuilds; `refresh_tenant_schema` didn't. The worker `teardown_schema` task got state reconciliation; the MCP `teardown_schema` tool didn't. View names got truncation guards (#227); tenant schema names didn't. The agent graph signature changed; the recipe runner didn't. This "fixed-where-it-bit" pattern is the single most damaging dynamic in the codebase.

3. **Roughly 27% of commits are fixes** (212/787), with fix-chains clustering on `apps/workspaces/tasks.py` (resume/janitor state machine: ~15 consecutive fix commits), `mcp_server/server.py`, and `apps/chat/views.py`.

4. **Several features are ~80% built and silently broken**: recipes cannot execute at all (stale graph signature → `TypeError`); shared threads always show zero artifacts (`conversation_id` never populated); PNG/PDF export returns 501 while its implementation sits unused; the role system has dead DRF permission classes and is unenforced outside membership management.

The complexity of multi-tenancy + shared tenant schemas + TTL lifecycle is *essential*. What is accidental is that the lifecycle rules live as prose comments scattered across five files instead of as one owned module, so every change re-derives them and misses a consumer.

---

## 2. As-built architecture map

- **Platform DB (Django ORM)**: users/tenants/memberships (`apps/users`), workspaces + schema lifecycle rows (`apps/workspaces`), chat threads/jobs (`apps/chat`), artifacts, recipes, knowledge, transformations. Also hosts the LangGraph checkpointer tables and Procrastinate queue.
- **Managed DB (raw psycopg)**: physical tenant schemas (`t_<sanitized external_id>` … or whatever `_sanitize_schema_name` yields), per-workspace UNION-view schemas (`ws_<hash>`), per-schema read-only roles. `SchemaManager` (`apps/workspaces/services/schema_manager.py`) owns DDL.
- **Chat path**: `POST /api/chat/` (`apps/chat/views.py`) → `build_agent_graph` (`apps/agents/graph/base.py`) → LangGraph loop with MCP tools (HTTP to `mcp_server/`) + local tools (artifacts, learnings, recipes). Server-side injection of `workspace_id`/`user_id`/`thread_id` into MCP tool args. SSE translation in `apps/chat/stream.py`. Conversation state in PostgreSQL checkpointer.
- **MCP server** (`mcp_server/server.py`, FastMCP, separate process, same Django ORM): `query`, `list_tables`, `describe_table`, `get_metadata`, `run_materialization`, `get_schema_status`, `teardown_schema`, `cancel_materialization`, `get_lineage`. No transport auth; trusts injected args; envelope + audit-log decorator.
- **Background work**: Procrastinate tasks in `apps/workspaces/tasks.py` — `materialize_workspace` (chat-triggered, with `ThreadJob` + `resume_thread_after_materialization` chaining), `refresh_tenant_schema` (Data-Dictionary refresh button), TTL janitor `expire_inactive_schemas`, ThreadJob janitor `expire_stale_thread_jobs`, view-schema rebuild/teardown tasks.
- **Materializer** (`mcp_server/services/materializer.py`, 1972 LOC): provision → discover → load (per-source, resumable for Connect) → transform (dbt) with per-source state in `MaterializationRun.result`.
- **Frontend**: React/Vite SPA (`frontend/`), Redux-ish slices (note: still named `domainSlice` — pre-rename residue), workspace pages, data dictionary, artifacts panel (sandboxed iframe served by Django), recipes/knowledge pages, public share pages.

---

## 3. Findings

### F1 — Schema refresh loads fresh data into the *old* schema, then schedules that schema for destruction (data loss)

**Status: BROKEN-NOW · Impact: data-loss · Confidence: verified-by-trace · Complexity: accidental**

Chain:
1. UI: Data Dictionary refresh → `api.post('/api/workspaces/${id}/refresh/')` — `frontend/src/store/dictionarySlice.ts:197`.
2. `RefreshSchemaView.post` creates a **new** `TenantSchema` named `{sanitized}_r{hex8}` via `create_refresh_schema` and defers the task — `apps/workspaces/api/views.py:362-365`, `apps/workspaces/services/schema_manager.py:169-181`.
3. `refresh_tenant_schema` creates the new physical schema, then calls `run_pipeline(membership, credential, pipeline_config)` **without passing the new schema** — `apps/workspaces/tasks.py:150,173`.
4. `run_pipeline` ignores the refresh schema entirely: `tenant_schema = SchemaManager().provision(tenant_membership.tenant)` — `mcp_server/services/materializer.py:183`. `provision()` resolves by the *unsuffixed* sanitized name and returns the **old ACTIVE schema** (`schema_manager.py:66-77`). All data is loaded into the old schema (`schema_name = tenant_schema.schema_name`, materializer.py:184).
5. The task then marks the **empty** `_r` schema ACTIVE (`tasks.py:182-184`) and flips every other ACTIVE schema — including the one that just received the fresh data — to TEARDOWN with a 30-minute delayed `teardown_schema` (`tasks.py:188-197`), which `DROP SCHEMA ... CASCADE`s it.

Net effect: 30 minutes after a successful refresh, the tenant's only ACTIVE schema is empty; the loaded data is dropped; `MaterializationRun` rows are attached to the dropped schema and flipped STALE, so the catalog reports no tables and the agent tells the user to re-materialize. (Recovery via chat materialization works but leaves two ACTIVE schemas, one empty.) This matches the v1 example finding `matlc-003`; as of this read the code is unchanged.

### F2 — Recipes cannot execute: runner targets a graph signature that no longer exists

**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental (contract drift)**

Chain:
1. `POST /api/workspaces/<id>/recipes/<id>/run/` → `RecipeRunner(recipe, variable_values, user)` with no `graph` — `apps/recipes/api/views.py:107`.
2. `RecipeRunner.execute()` → `_build_graph()` → `build_agent_graph(tenant_membership=self._tenant_membership, user=..., checkpointer=None)` — `apps/recipes/services/runner.py:115-119`.
3. `build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)` has no `tenant_membership` parameter and `workspace` is required — `apps/agents/graph/base.py:480-486` → `TypeError` on every run.

Additional drift even if the call were fixed: the runner's `initial_state` uses `tenant_id`/`tenant_name`/`tenant_membership_id` (`runner.py:216-224, 302-311`) but `AgentState` is `workspace_id`/`user_id`/`user_role`/`thread_id` (`apps/agents/graph/state.py:80-148`); and the runner never passes `mcp_tools`, so the recipe agent would have no query tools. Side effect: `_create_run_record` runs before the crash, stranding `RecipeRun` rows in RUNNING. The frontend has a full Recipes UI (`frontend/src/pages/RecipesPage/`), so this is a user-visible 500.

### F3 — MCP `teardown_schema` tool drops physical schemas but leaves all Django state claiming they exist

**Status: BROKEN-NOW (when invoked) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental (stale sibling of the worker path)**

The agent-exposed tool `teardown_schema` (`mcp_server/server.py:802-865`) calls `mgr.ateardown_view_schema(vs)` and `mgr.ateardown(ts)`, which are documented as physical-DROP-only ("callers are responsible for updating the model state", `schema_manager.py:183-193, 474-492`) — and then **never updates any state**: `TenantSchema`/`WorkspaceViewSchema` rows stay ACTIVE, `MaterializationRun` rows stay COMPLETED, dependent sibling view schemas aren't failed. Compare the worker task `teardown_schema` (`apps/workspaces/tasks.py:609-663`), which does all four. After the tool runs, `get_schema_status` reports `exists: true, state: active`, the catalog lists ghost tables, and queries fail with "relation does not exist". Tenant schemas are shared across workspaces, so the tool also silently destroys *other* workspaces' data, with no record. Reachable: the tool is in `MCP_TOOL_NAMES` and the LLM is instructed to call it on user request.

### F4 — Tenant identity keyed by bare `external_id` in hot paths; cross-provider collisions route one tenant to another tenant's schema

**Status: LATENT · Impact: security · Confidence: verified-by-trace (code path); collision occurrence probabilistic · Complexity: accidental**

`Tenant` is unique on `(provider, external_id)` (`apps/users/models.py:124`), and Connect opportunity IDs and OCS experiment IDs are both small numeric strings (`apps/users/services/tenant_resolution.py:99-103, 156-159`). But:

- `load_tenant_context` resolves by `tenant__external_id=tenant_id` only — `mcp_server/context.py:56-59`. Connect opp `123` and OCS bot `123` are indistinguishable; `.afirst()` returns whichever sorts first by `last_accessed_at`.
- `_sanitize_schema_name("123")` → `t_123` for **both** providers (`schema_manager.py:625-631`), and `provision()` matches an existing ACTIVE schema **by schema_name alone**, regardless of tenant (`schema_manager.py:66-71`) — so provisioning tenant B returns tenant A's live schema and the materializer writes B's data into it (`materializer.py:183-184`). Both tenants' users then query a mixed schema: cross-tenant data exposure.
- `build_view_schema` does `Tenant.objects.get(external_id=tenant_external_id)` — `schema_manager.py:306` — which raises `MultipleObjectsReturned` on any duplicate, failing every multi-tenant build involving that ID (and the objects were already in memory; the re-query is pure accident).

### F5 — Tenant schema names have no 63-byte guard (the truncation bug class was fixed for view names only)

**Status: LATENT · Impact: correctness/security · Confidence: strong-inference · Complexity: accidental**

PR #227 added careful byte-length and collision guards for view names (`schema_manager.py:29-31, 219-241, 324-350`). The sibling site was not fixed: `_sanitize_schema_name` (`schema_manager.py:625-631`) puts an unbounded `external_id` (model allows 255 chars) into `CREATE SCHEMA`; PostgreSQL silently truncates identifiers to 63 bytes, so two long external IDs sharing a 63-byte prefix get distinct `TenantSchema` rows but the *same physical schema* — the same bug class as the incident that prompted #227, one call site over.

### F6 — Roles exist but are barely enforced; the DRF permission classes are dead code

**Status: DEBT · Impact: security · Confidence: verified-by-trace (dead classes; spot-checked views) · Complexity: accidental**

`IsWorkspaceMember` / `IsWorkspaceReadWrite` / `IsWorkspaceManager` (`apps/workspaces/permissions.py`) have **zero imports anywhere**. Role checks are re-implemented inline in exactly three places: member management (`workspace_views.py:302,333,390,464,...`), `RefreshSchemaView` (`api/views.py:330`), and table annotation PUT (`api/views.py:500`). Everything else is membership-only: a `read`-role member can delete artifacts (`apps/artifacts/views.py:915-920` — no role check), create/edit knowledge, run recipes, and chat (which can trigger materialization and `teardown_schema` via the agent). Meanwhile `AgentState.user_role` is hardcoded to `"analyst"` at every call site and its docstring claims viewer/analyst/admin semantics that no code reads (`state.py:107-111`) — a comment/behavior mismatch.

### F7 — `Artifact.conversation_id` is never populated; shared threads always show zero artifacts; tool returns a dead `render_url`

**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`_build_tools` calls `create_artifact_tools(workspace, user)` without the `conversation_id` parameter (`apps/agents/graph/base.py:694`), so every chat-created artifact stores `conversation_id=""` (`apps/agents/tools/artifact_tool.py:197`). The only consumer filters `conversation_id=str(thread_id)` (`apps/chat/thread_views.py:65`), so `public_thread_view`'s `artifacts` array is always empty. Also, the tool returns `render_url = f"/artifacts/{id}/render/"` (`artifact_tool.py:209`), a route that does not exist (`apps/artifacts/urls.py` has `/api/workspaces/<id>/artifacts/<id>/sandbox/`). The thread→artifact link is a loose `CharField`, not an FK — typical of the stringly-typed couplings in this codebase.

### F8 — Live-query artifacts in multi-tenant workspaces execute against the wrong schema

**Status: BROKEN-NOW for multi-tenant workspaces · Impact: correctness · Confidence: strong-inference · Complexity: accidental (scoping re-derived locally)**

`ArtifactQueryDataView` resolves the query context via `tenant = await artifact.workspace.tenants.afirst()` + `load_tenant_context(tenant.external_id)` (`apps/artifacts/views.py:795-800`) instead of `load_workspace_context`. In a multi-tenant workspace the agent writes SQL against the `ws_*` namespaced views (`{prefix}__{table}`); executed with `search_path` set to one tenant's `t_*` schema, those queries fail ("relation does not exist") — every live artifact in a multi-tenant workspace renders an error.

### F9 — The single-tenant "compatibility shims" still drive whole features, silently degrading multi-tenant workspaces

**Status: DEBT (correctness for multi-tenant) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`Workspace.tenant` returns `tenants.first()` — ordered by `canonical_name`, i.e. the *alphabetically first* tenant (`apps/workspaces/models.py:143-146`, `users/models.py:123-125`). Call sites that operate only on that tenant: Data Dictionary list + table detail (`workspaces/api/views.py:241,245,479,483,506`), refresh trigger and status (`views.py:336,387` — the refresh button refreshes only one tenant of a multi-tenant workspace), knowledge export (`knowledge/api/views.py:254`), recipes TTL touch (`recipes/api/views.py:116`), recipe runner state (`recipes/services/runner.py:217-218`), artifact live queries (F8). None of these warn that they ignored the other tenants.

### F10 — Refresh path never rebuilds dependent view schemas; teardown comment asserts a falsehood on that path

**Status: LATENT (compounded by F1) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental (fixed-where-it-bit)**

`materialize_workspace` defers sibling view-schema rebuilds after re-materializing shared tenants (`tasks.py:346-349`, PR #230). `refresh_tenant_schema` — which also replaces a shared tenant schema — got no equivalent. Its teardown of the old schema runs `_fail_dependent_view_schemas` with the comment "We do NOT defer a rebuild: the tenant's data is gone" (`tasks.py:647-653`) — true for TTL expiry, **false** for refresh, where a new ACTIVE schema exists and a rebuild would succeed. Multi-tenant workspaces sharing a refreshed tenant stay FAILED until something else re-materializes.

### F11 — OAuth-token plumbing into MCP is vestigial; docstrings describe a transport that doesn't exist

**Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental (dead 80% feature)**

`mcp_server/auth.py:extract_oauth_tokens` has zero callers. `build_agent_graph(..., oauth_tokens=...)` accepts and ignores the argument (`graph/base.py:485` — referenced only in the docstring). `config["oauth_tokens"]` set in `chat/views.py:197` and `tasks.py:1155` is never read. `TenantContext.oauth_tokens` (`mcp_server/context.py:42`) is unused. Yet `auth.py`'s module docstring asserts "Tokens are injected by the Django chat view at the transport layer." The real credential flow is `aresolve_credential` in the worker. Every chat request still queries `SocialToken` for nothing.

### F12 — Two cancellation mechanisms with diverging semantics, one unscoped

**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental (same problem solved twice)**

MCP `cancel_materialization` sets `state=FAILED` with `result.cancelled=True` (`mcp_server/server.py:478-482`); the API cancel path sets `state=CANCELLED` (which `_aggregate_materialization_state` and the resume prompt treat as a distinct, user-friendly outcome — `tasks.py:991-1003`). A run cancelled via MCP reports "failed" downstream. Both `cancel_materialization` and `get_materialization_status` take a bare `run_id` with no workspace/user scoping (`server.py:407-493`), unlike every other tool.

### F13 — The ThreadJob/resume state machine is the largest fix-chain in the repo and still carries acknowledged hacks

**Status: DEBT · Impact: velocity (was correctness; now mostly patched) · Confidence: verified-by-trace · Complexity: essential problem, accidental construction**

`git log --follow apps/workspaces/tasks.py` shows ~15 consecutive fix commits on the resume/janitor lifecycle (`2fc7e45`, `bc82ab8`, `0833e7e`, `4f0def1`, `9818e4c`, `e451c7a`, `4b438d4`, `28b6647`, `00c423d`, …). The surviving code is careful but baroque: a sleep-retry hedge for a race MCP creates by committing `ThreadJob` after `defer_async`, with a TODO documenting the correct fix (placeholder row + nullable job id) that was "skipped for this PR" (`tasks.py:363-396`); CAS comments that exist because earlier versions clobbered states. The state machine works now, but its invariants live in comments across `tasks.py`, `server.py`, and `jobs_views.py` rather than in one owned module — the next change here will regress something again.

### F14 — Dead code, rename residue, and half-removed features

**Status: DEBT/COSMETIC · Impact: velocity · Confidence: verified-by-trace per item**

- `prune_messages`/`DEFAULT_MAX_MESSAGES` (`agents/graph/state.py:24-77`): zero callers. Consequence: **no message pruning actually happens** — full history (including up-to-500-row tool results) is replayed to the LLM every turn until the context overflows. Cost grows superlinearly per thread (cost-perf, not just hygiene).
- `get_postgres_checkpointer`/`get_sync_checkpointer` (`agents/memory/checkpointer.py`): only re-exported, never called; the live singleton is `apps/chat/checkpointer.py`. Two checkpointer modules, one real.
- `stream.py:187` audit-logs `input_state.get("project_id")` — field renamed away (#89); always empty. Frontend still has `domainSlice.ts`/`activeDomainId` naming from the same rename.
- Share UI was removed (`9783eb2 Remove public/share-creation UI`) but the share API (`thread_share_view`), public endpoints, and token generation remain live — a feature half-deleted in the same way others are half-built.
- `ArtifactExportView` returns 501 for PNG/PDF and points the caller at the same URL (`artifacts/views.py:979-987`); `export.py` contains the unused async `export_png`/`export_pdf` implementations.
- `Workspace.data_dictionary` "legacy" JSONField fallback still consulted in `TableDetailView._get_table_data` (`api/views.py:425-427`).
- `TODO.md` is stale in the opposite direction: it lists "PostgreSQL role isolation" as unimplemented while `query.py:44` does `SET ROLE` — the docs understate the system.

### F15 — System prompt and knowledge context grow without bound and are rebuilt per cache-miss

**Status: LATENT · Impact: cost-perf · Confidence: verified-by-trace · Complexity: accidental**

`KnowledgeRetriever.retrieve(user_question)` ignores `user_question` and concatenates **every** `KnowledgeEntry` and **every** `TableKnowledge` for the workspace into the system prompt (`knowledge/services/retriever.py:33-113`) — no budget, no relevance filter (only learnings are capped at 20). `_build_system_prompt` additionally runs `pipeline_describe_table` per table on every 60-second cache expiry (`graph/base.py:267-271`). As a workspace's knowledge grows, every chat turn's token bill grows with it.

### F16 — `TableKnowledge` is keyed by physical `{schema_name}.{table_name}`; annotations evaporate when schema names change

**Status: LATENT · Impact: correctness · Confidence: strong-inference · Complexity: accidental (stored-reference coupling)**

Annotations are stored under `qualified_name = f"{schema_name}.{table_name}"` (`workspaces/api/views.py:289-290, 524-527`). Refresh creates schemas named `{base}_r{hex}` (F1), and teardown/resurrection changes which physical name is live — at which point every annotation lookup (`_get_annotation`) misses and user-entered table documentation silently disappears from both the UI and the agent's Knowledge Base section. Same defect class as the known "materialization renamed tables and broke artifacts" incident: free-text references to physical schema objects with no migration story.

### F17 — MCP server trusts the network entirely

**Status: DEBT · Impact: security · Confidence: verified-by-trace (code); deployment exposure unverified · Complexity: mixed**

The MCP HTTP transport has no authentication (`mcp_server/server.py:904-914` — DNS-rebinding allowlist only); every tool fully trusts `workspace_id`/`user_id` arguments. Inside chat these are injected server-side (`graph/base.py:439-477`), which is good — but anything that can reach port 8100 can run `query` against any workspace or `teardown_schema(confirm=True)` against any workspace. The `run_materialization` authz helper also passes when `user_id=""` (any membership in the workspace, `server.py:509-515`). Defense rests entirely on network topology; that assumption is documented only in a comment.

### F18 — Chat access model is inconsistent between single- and multi-tenant workspaces

**Status: DEBT · Impact: correctness (access-model coherence) · Confidence: verified-by-trace · Complexity: mixed**

`_resolve_workspace_and_membership` requires a `TenantMembership` for single-tenant workspaces but only a `WorkspaceMembership` for multi-tenant ones (`apps/chat/helpers.py:88-120`). So inviting someone to a 1-tenant workspace is insufficient for chat, but inviting them to a 2-tenant workspace grants full query access to data materialized under other users' credentials. Either rule may be the intended sharing model; having both is incoherent and exactly the kind of branching the review brief flags.

### F19 — Account merge gate requires verified emails that the system never verifies

**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`reconcile_existing_user_on_login` refuses to merge unless the canonical user has a **verified** `EmailAddress` (`apps/users/signals.py:104-115`), but `ACCOUNT_EMAIL_VERIFICATION = "optional"` (`config/settings/base.py:199`) and nothing in the product flow drives verification. Password-created accounts therefore can never be auto-merged when the same person later arrives via OAuth — the documented orphaning symptom. The merge machinery itself (`users/services/merge.py`) is thorough; the gate condition just references a state the system doesn't produce. (Fail-closed is defensible security-wise; then the gap is the absent verification flow, not the gate.)

### F20 — `commcare_sync` as a hardcoded universal fallback

**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

Four call sites fall back to `registry.get("commcare_sync")` when pipeline resolution fails — including for OCS/Connect tenants and for multi-tenant view schemas (`graph/base.py:241`, `workspaces/api/views.py:271,451`, `mcp_server/server.py:101`). Wrong-provider metadata is silently used rather than surfacing "unknown pipeline".

---

## 4. Cross-cutting patterns

1. **Fixed-where-it-bit**: every incident fix landed at the bleeding site only — truncation (views fixed, schema names not, F5), sibling rebuilds (`materialize_workspace` fixed, refresh not, F10), teardown state reconciliation (worker task fixed, MCP tool not, F3), TTL touch (provision fixed in #228; the refresh path's empty-schema problem untouched, F1).
2. **Scoping re-derived locally instead of through one resolver**: `load_workspace_context` exists and is correct, but artifacts (F8), data dictionary/refresh/knowledge/recipes (F9), and chat helpers (F18) each re-derive workspace→schema/tenant logic with different (often single-tenant-only) results.
3. **Stringly-typed references with no migration story**: `Artifact.conversation_id` (F7), `TableKnowledge.table_name` = physical qualified name (F16), `artifact.source_queries` SQL against physical schemas, `render_url` literals (F7). When physical names move, these all break silently.
4. **Contract drift across process/feature seams**: recipe runner ↔ graph signature (F2), MCP tool ↔ worker task semantics (F3, F12), docstring ↔ behavior (oauth tokens F11, `user_role` F6, teardown comment F10).
5. **Comments as the system of record**: the shared-tenant-schema lifecycle invariants exist only as long prose comments in `tasks.py`/`server.py`. Each is accurate for the path it sits on and wrong one path over.
6. **Dead scaffolding from abandoned passes**: permissions.py, prune_messages, memory/checkpointer, oauth plumbing, export png/pdf, share UI — each an ~80% feature whose remaining 20% was never scheduled.

---

## 5. Recommendations (prioritized)

1. **Now — stop the data loss (F1, F3, F10).** Make `run_pipeline` accept an explicit target `TenantSchema` (kill the hidden `provision()` inside it), or delete the blue/green `_r` refresh design entirely and make refresh = `materialize_workspace` for one tenant. Make the MCP `teardown_schema` tool delegate to the worker task. Small, surgical; unblocks trusting the refresh button. (~1–2 days each.)
2. **Now — fix the two dead user-facing features (F2, F7).** Recipes: rewrite `_build_graph`/`initial_state` against the current graph contract, pass `mcp_tools`, add one integration test that actually invokes the runner (the current breakage proves none exists). Artifacts: pass `thread_id` into `create_artifact_tools` and fix `render_url`. (~1 day each.)
3. **Single scoping module (F4, F8, F9, F18).** One function answers "workspace → query context" and one answers "workspace → tenants for feature X"; ban `workspace.tenant`, `tenants.first()`, and bare-`external_id` lookups via a lint rule (the shims should raise on multi-tenant workspaces rather than silently picking alphabetically). Add `provider` to every tenant lookup. (~1 week, unblocks multi-tenant correctness everywhere.)
4. **Schema-lifecycle ownership (F5, F13, F16).** Move all lifecycle transitions (provision/refresh/teardown/TTL/view-rebuild) behind one service with the invariants as code (state-transition table + length/collision guards on *all* identifiers), not comments. Key `TableKnowledge` and other stored references by logical (tenant, table) not physical schema name, with a migration. (~2 weeks.)
5. **Decide the role model (F6).** Either enforce the three roles via the existing (currently dead) permission classes across artifacts/knowledge/recipes/chat, or delete `user_role` and the classes and document membership-only. Half-built RBAC is worse than none for review confidence.
6. **Guardrails.** (a) CI grep for zero-caller exports (would have caught permissions.py, prune_messages, extract_oauth_tokens); (b) PR checklist item: "does this change have siblings?" (refresh/materialize, tool/task, sync/async pairs); (c) keep expanding the ruff-as-guardrail approach (`08b4673`) — it demonstrably works here; (d) one end-to-end test per user journey (refresh→query, recipe run, shared thread) — every BROKEN-NOW above would have been caught by the most basic such test.
7. **Later — cost/scale (F15, F14-pruning).** Budget the knowledge section, implement real message pruning/summarization, stop rebuilding full schema context per 60s.

---

## 6. What's actually fine

- **SQL execution path** (`mcp_server/services/sql_validator.py`, `query.py`): sqlglot AST validation, single-statement, dangerous-function blocklist, LIMIT injection/capping, `SET ROLE <schema>_ro` + `statement_timeout` — real defense in depth, cleanly layered.
- **Chat endpoint security** (`apps/chat/views.py`): CSRF, rate limiting, message length caps, thread-ownership validation with 404-not-403, the stale/foreign-thread recovery (#231/#232) — coherent.
- **`merge_users`** (`users/services/merge.py`): transactional, handles conflicts per relation, discovers long-tail FKs via `_meta` — well above typical quality.
- **`workspace_service.add/remove_workspace_tenant`**: correct locking (`select_for_update` with the aggregate caveat documented), transaction-safe defers.
- **Post-#227 view-name guards in `build_view_schema`**: byte-accurate, collision-checked before any DDL, idempotent drop-and-recreate. (The lesson just needs to be applied to schema names too.)
- **The SSE stream translator** (`chat/stream.py`) and the MCP envelope/audit decorator (`mcp_server/envelope.py`): consistent shapes, sensible truncation, token scrubbing.
- **Async conventions**: the codebase genuinely follows its own CLAUDE.md async rules; the worker DB-resilience work (#224) and CAS-on-state-transition idioms are careful.

---

## 7. Coverage log

**Deep-read (line-by-line):** `apps/workspaces/{models,tasks,permissions,workspace_resolver}.py`, `apps/workspaces/services/{schema_manager,workspace_service}.py`, `apps/workspaces/api/views.py`, `apps/users/models.py`, `apps/users/services/{merge,tenant_resolution,credential_resolver}.py`, `apps/users/signals.py`, `apps/chat/{views,thread_views,helpers,models,stream}.py`, `apps/agents/graph/{base,state}.py`, `apps/agents/mcp_client.py`, `apps/agents/tools/artifact_tool.py`, `apps/artifacts/{models,views,urls}.py`, `apps/recipes/services/runner.py`, `apps/knowledge/services/retriever.py`, `mcp_server/{server,context,auth}.py`, `mcp_server/services/{sql_validator,query}.py`, `mcp_server/services/materializer.py` (lines 100–420 only), `config/urls.py`, `config/middleware/embed.py`, `mcp_server/envelope.py` (lines 60–116).

**Skimmed (outline/grep/partial):** `apps/workspaces/api/workspace_views.py` (first 120 lines + role-check greps), `apps/workspaces/api/{jobs_views,materialization_views}.py` (outlines), `apps/recipes/api/views.py` (run-view section), `apps/knowledge/api/views.py` (outline), `apps/transformations/*` (outlines only), `apps/users/views.py` (outline), `apps/chat/checkpointer.py` (head), `apps/agents/memory/checkpointer.py` (outline), `apps/artifacts/services/export.py` (outline), `config/settings/base.py` (targeted greps), frontend (directory structure, `dictionarySlice` refresh call, store/router/page inventory), `TODO.md`, git history (`--oneline` full, `--follow` on tasks.py, churn counts).

**Not examined at all:** all `mcp_server/loaders/*` (20 files — Connect/OCS/CommCare API loaders, pagination, retry, cursor logic), `mcp_server/services/materializer.py` lines 420–1972 (writers, commit semantics, dbt phase, cursor persistence), `mcp_server/services/{dbt_runner,metadata}.py` bodies, `mcp_server/pipeline_registry.py`, `pipelines/` YAML definitions, `apps/transformations/` service bodies (executor, lineage, commcare_staging SQL generation — `_sql_escape` would merit a security look), `apps/users/{auth_views,adapters,rate_limiting,decorators}.py` and OAuth provider modules, `apps/users/services/{token_refresh,ocs_team}.py`, `apps/chat/{message_converter,rate_limiting,constants}.py` bodies, `apps/agents/{tracing,prompts/*,tools/learning_tool,tools/recipe_tool}.py`, `apps/recipes/models.py` (template rendering — injection surface unreviewed), `apps/knowledge/{models,utils,management}`, all `admin.py`, all migrations, `tests/` and `tests/qa` (test architecture unassessed), frontend component/page/slice implementations, `config/settings/{production,development,test,connectlabs}.py`, `config/{procrastinate,taskbadger,views}.py` bodies, `infra/`, Dockerfiles, `docker-compose*.yml`, `.kamal`/deploy configs, `scripts/`, `templates/`, `apps/common/`.
