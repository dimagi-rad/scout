# Lens report: dead code, vestige & rename residue

*Reviewer: lens-dead-code · 2026-06-12 · HEAD 35e4230*
*Mandate: zero-caller functions, unused models/fields, stale docstrings, rename residue, compatibility shims and who stands on them. Zero-caller claims proven by repo-wide grep (apps/, mcp_server/, config/, tests/, frontend/src). Vulture (min-confidence 60) and knip used as candidate generators; every reported item re-verified by grep — framework-registered false positives (admin classes, DRF methods, FastMCP `@mcp.tool` functions, cron tasks) were discarded.*

---

## Headline: one piece of "dead-signature" residue is a live outage

### F1 — Recipe execution calls `build_agent_graph` with a signature removed in March; every recipe run 500s and leaves an orphaned RUNNING row  — **BROKEN-NOW / correctness / verified-by-trace**

The runner was never migrated when the graph API moved from `tenant_membership` to `workspace` (commit `e26cd75`, 2026-03-10, "Phase 9 — multi-tenant workspaces"). The runner's last functional touch predates that (`1af748d`, #94).

Chain (entry → consequence):

1. UI: Recipes page run action → `frontend/src/store/recipeSlice.ts:135` — `api.post(.../recipes/${recipeId}/run/)`
2. Route: `apps/recipes/urls.py:23` → `RecipeRunView`
3. `apps/recipes/api/views.py:107-108` — `runner = RecipeRunner(recipe=..., variable_values=..., user=...)` (no `graph=` provided) then `run = runner.execute()`
4. `apps/recipes/services/runner.py:188-190` — `self._run = self._create_run_record()` (writes a `RecipeRun` with `status=RUNNING`, `started_at=now`) **then** `graph = async_to_sync(self._build_graph)()`
5. `apps/recipes/services/runner.py:115-119`:
   ```python
   self._graph = await build_agent_graph(
       tenant_membership=self._tenant_membership,
       user=self.user,
       checkpointer=None,
   )
   ```
6. `apps/agents/graph/base.py:480-486`:
   ```python
   async def build_agent_graph(
       workspace: Workspace,
       user: User | None = None,
       checkpointer: BaseCheckpointSaver | None = None,
       mcp_tools: list | None = None,
       oauth_tokens: dict | None = None,
   ):
   ```
   → `TypeError: build_agent_graph() got an unexpected keyword argument 'tenant_membership'` (and missing positional `workspace`).
7. `apps/recipes/api/views.py:109-111` — bare `except Exception` → HTTP 500 `{"error": str(e)}`. The `RecipeRun` row created in step 4 is **never updated**: it stays `RUNNING` forever (no janitor covers RecipeRun).

Why tests don't catch it: every `runner.execute()` test patches the symbol with a bare `Mock` — `tests/test_recipes.py:599,624,650,678,…` `@patch("apps.recipes.services.runner.build_agent_graph")` — and `Mock` accepts any kwargs, so the signature mismatch is invisible. (This is the same mock-masks-contract pattern project memory records for `aresolve_credential`.)

Secondary drift in the same file, relevant even after the signature is fixed: `runner.py:215-222` builds the initial state with the **pre-workspace state shape** — `tenant_id`, `tenant_name`, `tenant_membership_id`, no `workspace_id` — while `AgentState` (`apps/agents/graph/state.py:138-148`) has `workspace_id`/`user_id`/`user_role`/`thread_id`, and `graph/base.py:503-505` injects `workspace_id` from state into every MCP tool call. A "fixed" runner would still issue MCP calls with an empty `workspace_id`. The runner also hardcodes `"user_role": "analyst"`.

Reachable via: Recipes UI (`RecipesPage` run flow) and `POST /api/workspaces/<id>/recipes/<id>/run/`. Complexity: accidental.

---

## Dead modules and zero-caller functions (each proven by grep)

### F2 — `apps/workspaces/permissions.py` is a complete dead module — **DEBT / security / verified-by-trace**

`IsWorkspaceMember`, `IsWorkspaceReadWrite`, `IsWorkspaceManager` (the entire file, 41 LOC) have **zero importers** anywhere:

```
$ grep -rn "IsWorkspaceMember\|IsWorkspaceReadWrite\|IsWorkspaceManager" --include="*.py" .
apps/workspaces/permissions.py:21/28/36   (definitions only)
```

All DRF views use `IsAuthenticated` only. This confirms both v1 runs: the role model (`WorkspaceRole`, `WorkspaceMembership.role`) is stored but the enforcement layer was built and never wired. Impact is security-shaped because the dead module *looks like* enforcement exists. (Route-level enforcement itself is the authz lens's territory; from this lens: dead code that advertises a control that isn't applied.)

### F3 — The OAuth-token-to-MCP plumbing is vestigial end to end; tokens are gathered, passed into an ignored kwarg and an unread config key, and the receiving extractor has zero callers — **DEBT / security / verified-by-trace** (token-persistence side effect: hypothesis)

Producer side:
- `apps/chat/views.py:162` — `oauth_tokens = await get_user_oauth_tokens(user)`
- passed to `build_agent_graph(..., oauth_tokens=oauth_tokens)` (`views.py:172,184`) — but inside `graph/base.py` the parameter appears **only** in the signature (line 485) and docstring (line 495); zero reads.
- also placed in `config["configurable"]["oauth_tokens"]` (`views.py:196`; resume path `apps/workspaces/tasks.py:1154`) — `grep -rn "configurable" apps/agents/` returns **zero hits**: nothing in the agent layer reads any configurable key.
- `apps/agents/mcp_client.py` creates `MultiServerMCPClient` with URL only — no headers, no `_meta` injection anywhere.

Consumer side:
- `mcp_server/auth.py:13` `extract_oauth_tokens` — zero production callers (tests only: `tests/test_mcp_server.py:212-225`). Its module docstring, "Tokens are injected by the Django chat view at the transport layer", describes a mechanism that does not exist in the codebase today.
- Actual credentials for materialization are resolved worker-side via `aresolve_credential`/`TenantConnection` (post-PR #220 model), which is why nothing noticed the dead pipe.

Side effect worth a verifier: `config.configurable.oauth_tokens` is part of the run config handed to a checkpointed LangGraph run (`views.py:196`, `tasks.py:1154`). If LangGraph persists run config into checkpoint rows, **live OAuth access tokens are being written to the platform DB** for no functional benefit. I did not trace langgraph-checkpoint-postgres internals — flagging as *hypothesis* for the security lens/verifier.

### F4 — `apps/agents/memory/checkpointer.py`: both checkpointer factories are dead; the live one lives in `apps/chat/` — **DEBT / velocity / verified-by-trace**

```
$ grep -rn "get_postgres_checkpointer\|get_sync_checkpointer" apps/ mcp_server/ tests/ config/  (excl. defining module)
→ no output
```

Only `get_database_url` is imported (by `apps/chat/checkpointer.py:10`). The real checkpointer singleton is `apps/chat/checkpointer.py:ensure_checkpointer` (used by chat views, thread views, and the resume task). ~120 of 166 LOC plus the `apps/agents/memory/__init__.py` re-exports are a dead parallel implementation — exactly the "same problem solved twice" residue the consistency lens hunts, except one side has no callers at all.

### F5 — `prune_messages` is dead, and therefore **no message pruning exists anywhere** — **DEBT / cost-perf / verified-by-trace**

`apps/agents/graph/state.py:24-77` (54 LOC incl. detailed docstring + `DEFAULT_MAX_MESSAGES`): zero callers (the only other mention is its own docstring example; `DEFAULT_MAX_MESSAGES` referenced nowhere else). No trimming/pruning exists in `graph/base.py` either (grep for trim/prune/max_messages: only tool-schema "trimmed_props"). Consequence: the docstring advertises a conversation-window strategy the system does not have; thread history grows unboundedly into both the checkpointer and the per-turn token bill. The dead function hides the absence.

### F6 — Backend artifact export surface is orphaned: endpoint has no UI caller, PNG/PDF are dead code requiring a dependency that isn't installed — **DEBT / velocity / verified-by-trace**

- No frontend call to `/export/<format>/` exists (`grep -rn "export/" frontend/src` → only the knowledge export). The UI's "Export PDF" is client-side `window.print()` (`frontend/src/components/ArtifactPanel/ArtifactPanel.tsx:42-45`).
- `ArtifactExportView` (`apps/artifacts/views.py:935-988`, wired at `apps/artifacts/urls.py:45`) serves `html` if hand-called, and returns **501** for png/pdf with the stale comment `# PNG and PDF require async - return error for now / In production, this would use async views or background tasks` (views.py:978-979).
- `ArtifactExporter.export_png` / `export_pdf` (`apps/artifacts/services/export.py:373-470`, ~100 LOC): zero callers, and `playwright` is not in `pyproject.toml`, so they can only ever raise ImportError.

### F7 — `Artifact.create_new_version` is a dead duplicate of the versioning the artifact tool does inline — **DEBT / velocity / verified-by-trace**

`apps/artifacts/models.py:173`: zero callers (grep: definition only). The live path creates versions inline in `apps/agents/tools/artifact_tool.py` (`version=original.version + 1`). Two implementations of the version-bump invariant; one dead, one live — divergence risk if anyone "helpfully" starts using the model method.

### F8 — `mcp_server` zero-caller pair: `run_commcare_sync` shim and `execute_internal_query` (with a stale test comment claiming it's used) — **DEBT / velocity / verified-by-trace**

- `mcp_server/services/materializer.py:1964-1972` — section literally headed `# ── Backwards-compatible shim ──`; `run_commcare_sync` ("Legacy entry point") has zero callers anywhere.
- `mcp_server/services/query.py:96` `execute_internal_query` — zero production callers; `mcp_server/services/metadata.py:21` imports `_execute_async_parameterized` directly. Meanwhile `tests/test_mcp_tenant_tools.py:29-31` asserts in a comment: *"the helpers do `from mcp_server.services.query import execute_internal_query`"* and defines `PATCH_INTERNAL_QUERY = "mcp_server.services.query.execute_internal_query"` — a stale claim about code structure; the suite is exercising a function production no longer calls.

### F9 — `RecipeRunner.execute_async` is an 80-line unreferenced duplicate of `execute` — **DEBT / velocity / verified-by-trace**

`apps/recipes/services/runner.py:257+`: zero callers (`apps/recipes/api/views.py:108` uses sync `execute()`; tests use `execute()`). The pair already drifted once would be invisible — both contain the same step-result assembly copy-pasted. Ironic detail: in an async-first codebase the *async* variant is the dead one, and the live sync one wraps the graph build in `async_to_sync`.

---

## Vestigial designs still carrying model weight

### F10 — `RecipeStep` is a leftover multi-step design: no production writer, no production reader, false docstrings — **DEBT / velocity / verified-by-trace**

- `apps/recipes/models.py:218-279` (`RecipeStep` with `order`, `prompt_template`, `expected_tool`, `render_prompt`) — the only non-test, non-admin references are imports in `apps/recipes/admin.py`.
- No API path can create or return steps: `RecipeUpdateSerializer.fields = ["name", "description", "prompt", "variables", "is_shared"]` (`apps/recipes/api/serializers.py:76`); no step serializer exists.
- The runner renders `Recipe.prompt` only (`runner.py:195` `self.recipe.render_prompt(...)`); it never touches `recipe.steps`.
- `RecipeRun.current_step` (models.py:401) and `add_step_result` (models.py:406) — test-only callers (`tests/test_recipes.py:527+`).
- Stale docs: `models.py:4-5` "Defines Recipe, RecipeStep, and RecipeRun models…", RecipeStep docstring "Steps are executed in order…" — describes behavior that does not exist; `models.py:25` still says a recipe "belongs to a project".

### F11 — `Workspace.data_dictionary` / `data_dictionary_generated_at` have **zero writers**; two readers are inert/dead branches — **DEBT / correctness / verified-by-trace**

Marked `# Legacy fields retained from the original per-tenant workspace model` (`apps/workspaces/models.py:131-133`). Grep for any assignment finds only `purge_synced_data` clearing them to `None`. Standing on the corpse:
- `apps/agents/tools/learning_tool.py:168-180` — "Validate tables exist in the workspace's data dictionary": `dd = workspace.data_dictionary or {}` → `known_tables` is always empty → the entire validation block is inert. The save_learning tool's table validation silently never runs.
- `apps/workspaces/api/views.py:425-426` — "Fallback: legacy data_dictionary JSONField" in table-detail: a fallback to a field nothing populates; dead branch.

### F12 — `Workspace.tenant` / `.external_tenant_id` / `.tenant_name` compatibility shims silently mean "first tenant" under 10+ call sites, including multi-tenant workspaces — **LATENT / correctness / strong-inference**

The properties are explicitly labeled shims (`apps/workspaces/models.py:143-165`: "Single-tenant compatibility: returns the first associated tenant", "Compatibility shim…"). Callers include `apps/knowledge/api/views.py:254`, `apps/workspaces/api/views.py:241,245,336,387,479,483,506`, `apps/recipes/api/views.py:116` (TTL touch on user recipe runs touches **only the first tenant's** schema). In a multi-tenant workspace `tenants.first()` is an arbitrary pick (default ordering). Each call site needs an owner to decide single-tenant-only vs. bug; I did not trace all ten to consequences — hence strong-inference. The lens point: a shim built for transition has become a load-bearing ambiguity.

---

## Share-surface drift (UI removed 2026-06-04, commit `9783eb2`)

### F13 — `PublicRecipePage.tsx` is a dead component that fetches an endpoint that does not exist — **DEBT / velocity / verified-by-trace**

- `frontend/src/pages/PublicRecipePage.tsx` — zero importers (`App.tsx` routes only `/shared/runs/` → `PublicRecipeRunPage` and `/shared/threads/` → `PublicThreadPage`, `App.tsx:21-22`); knip confirms unused file.
- It fetches `/api/recipes/shared/${token}/` (`PublicRecipePage.tsx:55`) — no such route exists anywhere in Django URLs (`config/urls.py` has only `api/recipes/runs/shared/<token>/`). Dead component aimed at a never-built endpoint.

### F14 — `Recipe.is_public` + `Recipe.share_token` are dead model fields: unwritable via API, unread by any endpoint — **DEBT / security / verified-by-trace**

- No serializer exposes them for write (`RecipeUpdateSerializer` fields exclude them; only `RecipeRun`'s update serializer has `["is_shared", "is_public"]`, `apps/recipes/api/serializers.py:113`).
- No public-recipe endpoint exists to read them (only `PublicRecipeRunView` for runs).
- The model's save() still mints `share_token = secrets.token_urlsafe(32)` whenever `is_public` is set (`models.py:117-120`) — token-minting machinery for a share flow with no producer and no consumer (Django admin could still set `is_public`, minting tokens that lead nowhere).

### F15 — Thread sharing: dead store action, UI-orphaned backend endpoint, and a frontend type field the backend never sends — **DEBT / security / verified-by-trace**

- `updateThreadSharing` (`frontend/src/store/uiSlice.ts:76-92`): zero component callers — the only way shares were created, removed with the share menu. So `PATCH /api/workspaces/<id>/threads/<id>/share/` (`apps/chat/thread_views.py:163-199`) is an API-only orphan, while `GET /api/chat/threads/shared/<token>/` (`thread_views.py:227`) remains public and serves any token minted before 2026-06-04 (plus messages **and all thread artifacts**, `thread_views.py:245`).
- Type drift: `ThreadShareState` and `Thread` in `uiSlice.ts:5-22` declare `is_public`, but the backend share payload is `{id, is_shared, share_token}` only (`thread_views.py:45-49`); the chat `Thread` model has no `is_public` at all (`apps/chat/models.py`).
- Net: a live unauthenticated read surface whose entire management surface is gone — nobody can audit or revoke shares except via DB/admin. The endpoints themselves are the authz lens's call; from this lens the management half is dead code and the types are residue.

(Recipe-run sharing is in the same half-state: `RecipeRunUpdateSerializer` still accepts `is_public`, `PublicRecipeRunView` + `PublicRecipeRunPage` are live, but no UI control sends `is_public` — only `is_shared` toggles remain, `RecipeDetail.tsx:207`, `RecipeRunDetail.tsx:207-209`.)

---

## Rename residue & stale docstrings (comments are claims; these are false claims)

### F16 — Audit log still logs `project_id`, which no longer exists in agent state — always empty — **DEBT / correctness / verified-by-trace**

`apps/chat/stream.py:182-188`:
```python
audit_logger.info(
    "tool_call tool=%s user_id=%s thread_id=%s project_id=%s",
    ..., input_state.get("project_id", ""),
)
```
`AgentState` has no `project_id` (fields: messages, workspace_id, user_id, user_role, thread_id — `state.py:138-148`); chat views populate `workspace_id` (`apps/chat/views.py:211`). Every tool-call audit record since the projects→workspaces rename (#89, 2026-03-17) carries an empty workspace dimension. One-line fix; matters for the next incident's forensics.

### F17 — Celery docstrings, 13 months after the Procrastinate migration — **COSMETIC / velocity / verified-by-trace**

- `apps/workspaces/api/views.py:319` — "dispatches a Celery task"
- `apps/workspaces/services/schema_manager.py:174` — "Celery task (refresh_tenant_schema)"
Both sit on the legacy `/refresh/` path (v1 S1 territory) — precisely where a maintainer needs accurate docs.

### F18 — Assorted false docstrings — **COSMETIC / velocity / verified-by-trace**

- `AgentState.user_role` docstring (`state.py:107-111`): claims viewer/analyst/admin "Controls" read-only vs. full access — no enforcement reads `user_role` anywhere (and the recipe runner hardcodes `"analyst"`).
- `setCachedUserTenants` (`frontend/src/api/userTenantsCache.ts:51`): "Used after add/remove mutations so the cache stays consistent" — zero callers; the claimed invariant is not maintained (whether connection add/remove actually leaves a stale tenants cache in the session is untested here — hypothesis, flagged for the frontend vertical).
- `mcp_server/auth.py:3-5`: "Tokens are injected by the Django chat view at the transport layer" — no such injection exists (see F3).
- `KnowledgeRetriever.retrieve(user_question="")` (`apps/knowledge/services/retriever.py:33`): parameter accepted and ignored; the sole caller passes nothing (`graph/base.py:731`) — retrieval is not question-aware despite the signature.
- "project" vocabulary residue in docstrings: `apps/artifacts/models.py:40`, `apps/artifacts/views.py:725,939`, `apps/recipes/models.py:25`, `apps/agents/prompts/artifact_prompt.py:106`.
- Frontend vocabulary residue: the whole store still speaks "domain" (`domainSlice.ts`, `activeDomainId` in ~15 files) for what the API calls workspaces and the UI calls data sources — three vocabularies for one concept.

### F19 — `CommCareCaseLoader` legacy `access_token` kwarg is kept alive only by tests — **COSMETIC / velocity / verified-by-trace**

`mcp_server/loaders/commcare_cases.py:30-34` ("Support legacy `access_token` kwarg for backwards compatibility"): the only callers passing it are `tests/test_commcare_loader.py:23,49`; production uses `credential=` (`materializer.py:761`). A compat path whose sole stakeholder is its own test suite.

---

## Frontend dead files/exports (knip, re-verified by grep)

### F20 — Dead frontend files and exports — **COSMETIC / velocity / verified-by-trace**

- `src/components/ui/dropdown-menu.tsx` — zero importers.
- `src/store/index.ts` plus barrels `AppLayout/index.ts`, `ChatMessage/index.ts`, `ChatPanel/index.ts`, `LoginForm/index.ts` — unused re-export shims (imports go direct to `store/store` / component files).
- `setCachedUserTenants` (`api/userTenantsCache.ts:51`) — dead export (see F18).
- `STARTER_QUESTIONS` (`ChatEmptyState/starterQuestions.ts:5`) — dead export (`getStarterQuestions` is the live path).
- `tests/e2e/scout-widget.d.ts` — unreferenced.
- ~26 unused exported types (knip list), including `ThreadShareState`/`Thread.is_public` drift noted in F15.
- Note: knip also flags `public/widget.js` as unused — **false positive**: Django serves it at `/widget.js` by reading `frontend/public/widget.js` from disk (`config/views.py:10`), and `COPY . .` in the Dockerfile with no `.dockerignore` ships it. Live, but invisible to frontend tooling — worth a comment in the file.

---

## Live compatibility shims standing under load (not dead; inventoried so they don't rot silently)

- `config/procrastinate.py:35-77` — connection-hygiene `task` decorator, explicitly `TEMPORARY: workaround for procrastinate#1134; upstream PR #1555 … strip this wrapper — see dimagi-rad/scout#225`. Currently load-bearing for every task; `tests/test_worker_db_resilience.py` enforces registration through it. Healthy shim with an exit ticket — the model for how the others should look.
- `apps/chat/helpers.py:7-12` — auth decorators re-exported "for backwards compat"; the only consumers are chat's own two view modules. Trivial to inline and delete.
- Legacy `/refresh/` route + `refresh_tenant_schema` (`apps/workspaces/tasks.py:126`, `api/views.py:365`) — **not dead**: wired to the Data Dictionary refresh button (`frontend/src/store/dictionarySlice.ts:197`). It is a vestigial second materialization path (v1 S1 data-loss territory — owned by the materialization vertical, noted here as live-vestige, not removable residue).
- `TenantCredential` residue is clean: name survives only in migrations 0001/0007/0008 and a migration test — correct places.

---

## What's fine (checked and healthy from this lens)

- **All 19 loader modules are imported** (per-module importer counts ≥2 excluding self); no orphan loaders.
- **`apps/common`** (8 LOC): `creator_display_name` has 4 production callers — not the empty husk the size suggests.
- **MCP envelope** (`success_response`/`error_response`/`Timer`/`scrub_extra_fields`): all used by server.py/query.py.
- **`TenantMetadata`**: written by the materializer discover phase (`materializer.py:539` `update_or_create`), read by server.py metadata tools and `graph/base.py` — alive both sides.
- **OCS allauth provider** (`apps/users/providers/ocs/`): registered in `INSTALLED_APPS` and used by signals/auth_views — vulture flags are framework false positives.
- **`extract`-style vulture hits in admin.py / apps.py / DRF serializers/views**: all framework-registered; discarded after spot-checks.
- **Slash command ↔ tool-name parity**: `/save-recipe` prompt references `save_as_recipe`; the tool is named exactly that (`recipe_tool.py:247`). `/refresh-data` references `run_materialization` — exists.
- **knowledge retriever, rate limiting, tracing, jobs endpoints**: all have live callers.
- **`reset_circuit_breaker`** (`mcp_client.py:67`): test-only by design and labeled as such — acceptable test hook.
- **Migration-test recreation of `TenantCredential`** (`tests/test_ocs_connections.py:85-97`): correct way to test a data migration whose model was dropped.

## Coverage log

**Deep-read (line-by-line or near):** `apps/workspaces/permissions.py`, `apps/chat/thread_views.py`, `apps/recipes/services/runner.py` (execute/_build_graph/init; execute_async skimmed), `apps/recipes/models.py` (Recipe/RecipeStep/RecipeRun sections), `apps/recipes/api/views.py` (run/public views), `apps/recipes/api/serializers.py` (field lists), `apps/agents/graph/state.py`, `apps/agents/mcp_client.py`, `apps/artifacts/services/export.py` (export_png/pdf + dispatch), `apps/artifacts/views.py` (export view section only), `config/procrastinate.py`, `config/views.py`, `apps/chat/checkpointer.py` (top), `mcp_server/auth.py`, `mcp_server/services/materializer.py` (shim + metadata-store sections only), `apps/workspaces/models.py` (Workspace legacy fields + compat properties), `apps/agents/tools/learning_tool.py` (validation block), `frontend/src/App.tsx`, `frontend/src/store/uiSlice.ts`, `frontend/src/api/userTenantsCache.ts`, `frontend/src/components/ChatPanel/slashCommands.ts`, `tests/test_recipes.py` (runner test section).

**Skimmed (greps + targeted snippets):** `apps/agents/graph/base.py`, `apps/chat/views.py`, `apps/chat/stream.py`, `apps/chat/helpers.py`, `apps/users/decorators.py`, `apps/knowledge/services/retriever.py`, `mcp_server/server.py` (run_materialization + tool list), `mcp_server/services/query.py`, `mcp_server/services/metadata.py`, `mcp_server/envelope.py`, `mcp_server/context.py`, `mcp_server/loaders/*` (import-graph only), `config/settings/base.py`, `Dockerfile`, `pipelines/*.yml`, `apps/users/services/merge.py` (result fields only), `apps/recipes/admin.py`, frontend via knip + greps (`recipeSlice`, `dictionarySlice`, `RecipesPage/*`, `ArtifactPanel`), vulture over `apps/ mcp_server/ config/`.

**NOT examined (honest gaps for the gap loop):**
- `apps/workspaces/tasks.py` body (1,289 LOC) and `services/schema_manager.py` — only greps; dead branches *inside* the task bodies unchecked.
- `mcp_server/services/materializer.py` bulk (1,972 LOC, 33 functions) — only the shim and metadata sections; per-table writer duplication/dead variants unaudited.
- `mcp_server/server.py` tool bodies other than `run_materialization`; `sql_validator.py`; `dbt_runner.py`; `pipeline_registry.py` internals.
- `apps/users/`: `views.py`, `auth_views.py`, `signals.py`, `adapters.py`, `tenant_resolution.py`, `token_refresh.py`, merge.py body — dead branches there unchecked (vulture showed nothing strong, but vulture misses dict-dispatch).
- `apps/transformations/` entirely (lineage/executor/commcare_staging may contain dead paths).
- `apps/artifacts/views.py` sandbox/data/query-data sections; `apps/knowledge/` views bulk.
- Frontend component bodies at scale (only knip-level + targeted files); unused *props/branches* inside components not assessed; `tests/qa/`, e2e specs.
- Migrations (beyond TenantCredential), `infra/`, `.kamal/`, deploy configs, management command bodies, Django admin usage in practice (admin-only affordances counted as "reachable via admin" without checking who uses admin).
- Whether LangGraph persists `configurable` into checkpoints (F3 hypothesis) — needs a verifier with langgraph-checkpoint-postgres knowledge.
