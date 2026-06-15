# Scout Architecture Review — Synthesis (canonical report)

*Phase 4 synthesis, 2026-06-12. Repo HEAD `35e4230`. Built from the verified-findings DB
(`findings/batch-00..14.json`, 148 adjudicated findings: 100 CONFIRMED, 46 PARTIAL,
2 REFUTED), the coverage logs (`coverage/batch-00..04.json`, 43 reviewers), the
cartography map (`cartography.md`), the contradiction-resolution pass
(`contradictions.md`), and the 43 full reports in `reports/`.*

*Citation convention: `[NN#i]` refers to `findings/batch-NN.json`, array index `i`.
`rK` = replication count (independent reviewers who found it). Verdicts are from
independent adversarial verification; PARTIAL means true-but-narrower than originally
claimed (the narrowed claim is what is stated here). The two REFUTED findings are
excluded from the body and listed in Appendix B. Anything not traceable to a finding is
explicitly marked **synthesis-level inference**.*

---

## 1. Executive summary

Scout's core single-tenant happy path — OAuth in, auto-created workspace, chat-driven
materialization, SQL via validated MCP tools under read-only roles — is genuinely solid
and incident-hardened. Around that spine, the review found **146 verified findings**
(31 BROKEN-NOW, 57 LATENT, 50 DEBT, 8 COSMETIC), including several whole features that
are shipped, reachable in the UI, and do not work at all.

The unsoftened headlines:

1. **The Data Dictionary "Refresh" button destroys the data it just loaded.** The
   refresh task loads into the *old* schema, activates an empty `_r` schema, then
   DROP-CASCADEs the data-bearing one — and the status API reports success. Found
   independently by 14 of the round-1 reviewers, double-verified. [00#0, r14]
2. **Recipes are 100% broken.** The runner calls `build_agent_graph` with a kwarg
   removed in March (`e26cd75`); every run is a TypeError → HTTP 500 → a RecipeRun row
   stranded RUNNING forever. The feature has been dead for ~3 months behind a working
   UI. [00#1, r11]
3. **Live artifacts in multi-tenant workspaces never render.** Query-data resolves the
   alphabetically-first tenant's schema instead of the workspace view schema the agent
   authored against. [00#6, r11]
4. **Role-based access control is ~unenforced on the content surface.** The DRF
   permission classes have zero importers; READ members can mutate artifacts,
   knowledge, recipes, and drive destructive agent tools through chat. Management
   endpoints do enforce MANAGE (contradictions.md C6) — content endpoints check
   membership only. [00#5, r8; 12#1]
5. **Three privilege-boundary holes**: the artifact sandbox runs agent-generated code
   same-origin with full session authority (`allow-scripts` + `allow-same-origin`
   neutralizes the sandbox) [02#1, r5]; transformation assets execute arbitrary user
   SQL via dbt as the managed-DB superuser with no validation or SET ROLE [04#3, r2];
   the MCP server has no caller authentication at all [01#6, r5].
6. **The tenant-identity keyspace is fragile**: bare `external_id` keying and missing
   63-byte guards on schema names allow cross-tenant schema collision — the same bug
   class as the 2026-06-10 incident, fixed only at the view-name site. [00#3, r7;
   00#4, r8]
7. **The platform tells comforting lies under failure.** Stream errors become normal
   text with `finishReason: "stop"` [06#4]; resume prompts tell the agent a fully
   FAILED materialization "just completed … using the now-loaded data" [14#5];
   checkpointer outages render as empty-but-successful UI [07#7]; view-schema cascade
   failures report a fabricated cause [07#9]. Dishonest success is a systemic pattern,
   not an isolated bug.
8. **Operations is flying blind**: zero CloudWatch alarms, a static health check, one
   worker process at concurrency 1 serializing all background work platform-wide, CI
   that never runs the incident-regression suites, and deploys not gated on tests.
   [08#7, r3; 08#2, r4; 12#2; 08#4]

The dominant complexity verdict is **accidental** (125/146): most findings are drift,
rename residue, fixes applied only where they bit, and N parallel implementations of
one truth — not inherent domain difficulty. The codebase's strongest areas (the
ThreadJob/materializer CAS discipline, SQL defense-in-depth, the post-incident
view-schema work) prove the team can do this well; the gap is that those standards are
applied at the sites that already burned, and almost nowhere else.

---

## 2. As-built architecture map

Condensed from the three independent generalist maps (reports/generalist-1/2/3.md §2),
which agree with each other and with cartography §4.

**Processes (5) + two DB planes + three external providers:**

```
React SPA (Vite, :5173/3000)            — Zustand slices; hand-written TS API types (no codegen)
   │ JSON + SSE
Django API (ASGI uvicorn :8000)         — auth, workspaces, chat SSE, artifacts, recipes,
   │                                       knowledge, transformations; LangGraph agent built
   │ MCP streamable-HTTP                   per request in-process
MCP server (FastMCP :8100)              — 11 tools; SAME Django ORM/codebase, separate process;
   │ psycopg direct                        materializer (1,972 LOC) lives here but runs in worker
Procrastinate worker (concurrency 1)    — all tasks in apps/workspaces/tasks.py + 2 cron janitors
   │
Platform DB ──────────────── Managed DB
 (Django models, checkpoints,  (t_* tenant schemas, ws_* view schemas, *_ro roles)
  procrastinate queue)          — same physical DB and same master credential in prod [11#0]
CommCare API · Connect API · OCS API    — OAuth/API-key via TenantConnection
```

**Chat turn**: `POST /api/chat/` → membership check → `build_agent_graph(workspace,
user, …)` (prefetches schema catalog + full knowledge dump into the system prompt,
60s per-process cache) → LangGraph agent↔ToolNode loop with panic-loop circuit breaker
→ MCP tools over HTTP with `workspace_id`/`user_id`/`thread_id` injected server-side
(hidden from the LLM) → `load_workspace_context` routes single-tenant→tenant schema,
multi-tenant→view schema → `query` under `SET ROLE <schema>_ro`. Conversation truth
lives in the LangGraph PostgreSQL checkpointer; `Thread` rows are an index.

**Materialization**: agent calls `run_materialization` (fire-and-ack) → defers
`materialize_workspace` + creates `ThreadJob` → worker runs `run_pipeline` per tenant
(provision → discover → load per source, per-page cursor watermarks for resumable
Connect sources → dbt transform) → rebuilds own + sibling view schemas → defers
`resume_thread_after_materialization`, which re-invokes the agent with a system-framed
status message. Two cron janitors (`expire_inactive_schemas` */30, TTL 24h;
`expire_stale_thread_jobs` */15) plus an API-side poll backstop reconcile state.

**Identity/tenancy**: allauth with three custom providers (CommCare, Connect, OCS) +
Google/GitHub; post-login tenant resolution upserts `Tenant`/`TenantMembership`/
`TenantConnection`; a `post_save` signal auto-creates a single-tenant MANAGE workspace.
Workspaces↔tenants M2M; roles read/read_write/manage on `WorkspaceMembership`.

**Content satellites**: artifacts (same-origin sandbox iframe; static `data` vs live
`source_queries`), recipes (prompt templates re-run through a fresh agent graph),
knowledge (KnowledgeEntry/TableKnowledge/AgentLearning → prompt), transformations
(dbt TransformationAssets + lineage), and a parallel DRF-sync data-dictionary API.

Things the map implies that the findings confirm matter: the MCP server and worker
write Django-modeled rows without sharing a process with Django's request lifecycle;
free-text SQL/table references are stored in six places and resolved by string match;
and "status" is derived independently by at least seven readers (§4, pattern 2).

---

## 3. Findings by subsystem

Format: `[id | verdict | STATUS/impact | rN | complexity]`. Evidence chains are in the
findings DB records (every CONFIRMED finding carries a quoted entry-point→consequence
trace; chains are abbreviated here to key files).

### 3.1 Materialization & schema lifecycle (the highest-stakes cluster)

**[00#0 | CONFIRMED | BROKEN-NOW/data-loss | r14 | accidental] Refresh destroys the
data it just loaded.** `refresh_tenant_schema` creates a `{base}_r{hex}` TenantSchema,
but `run_pipeline` ignores it and `SchemaManager().provision(tenant)` resolves the
*old* ACTIVE schema by base name — data loads there; the task then activates the empty
`_r` schema and schedules DROP SCHEMA CASCADE on the data-bearing one (+30 min).
During the 30-minute pre-DROP window exactly one ACTIVE row exists (the empty `_r`
schema), so routing deterministically targets the empty schema (the two-ACTIVE overlap
lasts only instants — verification corrected the original nondeterminism sub-claim);
dependent view schemas flip FAILED; `RefreshStatusView` reports success; and
`tests/test_refresh_task.py` mocks `run_pipeline` and *asserts the destructive
teardown*, pinning the broken contract. Chain: `dictionarySlice.ts:197` →
`api/views.py:362` → `tasks.py:150-197` → `materializer.py:183` →
`schema_manager.py:66-78` → `tasks.py:609-663`. Reachable today via the Data
Dictionary refresh button. The companion gap: the refresh path also never rebuilds
dependent sibling view schemas [00#9 | CONFIRMED | LATENT].

**[00#2 | CONFIRMED | BROKEN-NOW/data-loss | r7] MCP `teardown_schema` drops physical
schemas and updates no Django state** — TenantSchema rows stay ACTIVE over dropped
schemas, runs stay COMPLETED, sibling multi-tenant workspaces whose views were
cascade-dropped are never failed. The worker-side teardown task does all of this
correctly; the agent-exposed tool is the unfixed sibling.

**[00#3 | CONFIRMED | LATENT/security | r7] + [00#4 | CONFIRMED | LATENT/security |
r8] Tenant identity and identifier-length holes.** `provision()` resolves an existing
TenantSchema by `schema_name` alone (no tenant FK check), and `_sanitize_schema_name`
maps distinct external_ids to one name (`a-b`/`a_b`/`a.b` collide; cross-provider id
`123` collides). Separately, schema/role/refresh names have no 63-byte guard — the
#227 truncation fix was applied to view names only. Either path yields two tenants
sharing one physical schema: cross-tenant disclosure plus destruction on the next
materialization.

**Races and lifecycle gaps** (all on the `TenantSchema`/`MaterializationRun`/
`WorkspaceViewSchema` state machines):

- [03#0 | CONFIRMED | LATENT/data-loss | r2] `provision()` resurrects TEARDOWN/EXPIRED
  rows, but the already-queued teardown task drops them **unconditionally** — no state
  CAS at the drop site. A materialization racing the janitor (or running inside
  refresh's 30-min window) loads fresh data into a schema already scheduled to die.
- [03#3 | PARTIAL | LATENT/data-loss | r5] No per-tenant mutual exclusion: the
  dispatch guard is thread-scoped, so two workspaces sharing a tenant (or retry racing
  chat) can run concurrent DROP/CREATE/INSERT on one physical schema. (PARTIAL:
  concurrency-1 worker currently serializes most interleavings — the invariant is
  deployment config, not code.)
- [03#9 | CONFIRMED | BROKEN-NOW/correctness | r4] No janitor reconciles
  MaterializationRun rows after hard worker death; procrastinate 3.8's stalled-job
  rescue is never wired. Zombie `doing` jobs + frozen UI spinners (the known
  2026-05-30 class) remain unrescuable.
- [03#1 | CONFIRMED | LATENT | r3] Partial-failure/cancel mid-rematerialization leaves
  the workspace's own view schema cascade-dropped but recorded ACTIVE; the agent is
  told "partial, answer what you can" while every query fails.
- [03#2 | CONFIRMED | LATENT | r2] `build_view_schema` reactivates EXPIRED view-schema
  rows without resetting `last_accessed_at` — the exact 2026-06-10 incident-b
  mechanism, view-schema edition.
- [04#0 | PARTIAL | DEBT | r2] `run_pipeline` completion saves a provision-time
  `last_accessed_at`, rewinding the TTL clock by the run duration.
- [01#1 | PARTIAL | LATENT | r9] Three cancel mechanisms with diverging semantics; MCP
  `cancel_materialization` writes FAILED (not CANCELLED), never aborts the job, never
  stops the load.
- [02#9 | CONFIRMED | BROKEN-NOW | r2] The stale-job reconciler measures staleness from
  `ThreadJob.created_at` and checks the *materialize* job's status, so any
  materialization >10 min gets its healthy resume falsely failed, with a synthetic
  "interrupted (server restart)" message injected into the thread. The 3s jobs-poll
  makes this near-deterministic [05#6].
- [01#2 | CONFIRMED | DEBT | r7] ThreadJob is created *after* `defer_async` (the
  acknowledged `tasks.py:373` TODO race); the resume lifecycle is a 19-commit fix
  chain held together by a hedge poll + janitor.
- [03#5 | PARTIAL | DEBT | r4] `SchemaState.MATERIALIZING` has zero production writers
  (grep-verified; contradictions.md "Confirmations") while ~15 readers branch on it —
  the single-tenant "materialization already in progress" guard can never fire.
- [03#4 | CONFIRMED | DEBT | r3] `get_schema_status` reads an extinct result shape:
  single-tenant `tables` is always `[]` after success.
- [03#8 | CONFIRMED | COSMETIC | r2] Minor run-lifecycle drift (terminal-run progress
  writes, result clobber, post-COMPLETED guard raise, wrong `materialized_row_count`
  after resumed runs).
- [09#4 | CONFIRMED | DEBT | r1] `purge_synced_data` orphans WorkspaceViewSchema rows
  and physical `ws_*` schemas.
- [09#5 | CONFIRMED | DEBT/velocity | r3] The tenant-schema↔view-schema dependency
  graph has no owner: 6+ hand-maintained hooks; four live mutation paths (refresh, MCP
  teardown tool, transformations trigger, purge) lack required hooks; five inconsistent
  TTL keep-alive implementations.

### 3.2 Tenancy, identity, authz

- **[00#5 | CONFIRMED | BROKEN-NOW/security | r8] Roles unenforced on content.**
  `permissions.py` classes: zero importers (resolved against a dissenting report in
  contradictions.md C1; the dissent was wrong). Management endpoints enforce MANAGE
  correctly (C6) — but artifacts PATCH/DELETE/export, knowledge CRUD/import (prompt
  injection vector), recipes CRUD/run, jobs cancel, and chat-driven
  materialize/teardown gate on membership only. `test_workspace_permissions.py` has
  zero role assertions and dead role fixtures [12#1 | PARTIAL].
- [01#7 | CONFIRMED | DEBT/security | r5 | mixed] Membership asymmetry: multi-tenant
  chat waives the per-tenant `TenantMembership` check that single-tenant chat
  enforces; a user holding T1 alone gets SQL read over T2/T3 in an A+B+C workspace.
- [06#7 | PARTIAL | BROKEN-NOW/security | r3] `archived_at` honored at ~3 of 8+ authz
  sites; tenant resolution is additive-only, so upstream-revoked access stays readable
  (reads need no credential).
- [01#8 | PARTIAL | DEBT | r6 | mixed] Merge-gate gap, narrower than originally
  claimed: OAuth logins that return an email *do* create verified EmailAddress rows
  (`VERIFIED_EMAIL=True` + allauth's `setup_user_email` chain), so OAuth-to-OAuth
  email collisions auto-merge fine; only password-signup-then-OAuth accounts fail the
  verified-email gate (local signup bypasses allauth, creating no EmailAddress row)
  and require an operator merge. Operationally resolved per project memory; the path
  remains subtle.
- [11#4 | CONFIRMED | DEBT/security | r1] `merge_users` OR-propagates
  `is_staff`/`is_superuser` from the deleted duplicate, invisible at the y/N prompt —
  plausibly already production state given the 2026-06 merge.
- [04#1 | PARTIAL | LATENT | r2] Merge conflict path cascade-deletes the duplicate's
  TenantMetadata; sibling pattern in users migration 0004 [11#9 | PARTIAL].
- **The second auth perimeter**: `/accounts/` (stock allauth HTML) is live in
  production — open self-registration with divergent EmailAddress state [13#9 |
  CONFIRMED | LATENT/security], bypasses the custom rate limiter and splits the
  brute-force budget [14#1 | CONFIRMED], and `SOCIALACCOUNT_LOGIN_ON_GET=True` +
  `EMAIL_AUTHENTICATION_AUTO_CONNECT=True` enables login-CSRF/silent account-linking
  on GET [14#2 | CONFIRMED | mixed]. No working email backend in production also kills
  password reset [14#0 | PARTIAL].
- [07#2 | PARTIAL | DEBT/security | r3] OCS/Connect email-domain allowlist open by
  default; `.env.example` documents the opposite; with auto-signup any
  OCS-authenticated user self-provisions an account.
- [07#4 | PARTIAL | LATENT/cost-perf | r1] `me_view` recomputes onboarding with no
  cache guard (unlike `tenant_list_view`'s 1h TTL), eagerly resolving all three
  provider APIs per page load — not per poll; the SPA does not poll `/me` — for a
  token-bearing user with zero persisted memberships, and `onboarding_complete` flaps
  transiently True while persisted state stays incomplete.
- [11#3 | CONFIRMED | LATENT/data-loss | r1] Django admin is an unguarded
  privileged-write surface: TenantSchema state/`last_accessed_at` fully editable (can
  re-arm DROP SCHEMA CASCADE), unthrottled login, plaintext tokens visible,
  self-escalation via UserAdmin; inverted registration — dangerous rows editable while
  every model operators actually need is absent [11#5 | CONFIRMED].
- [07#1 | PARTIAL | LATENT | r2] Cross-workspace cancellation via shared-tenant orphan
  selection.
- [06#5 | CONFIRMED | LATENT | r2] Zero-tenant workspaces are creatable and dead-end in
  chat with a generic error.

### 3.3 Chat, agent graph, streaming, checkpointer

- [01#0 | CONFIRMED | DEBT/velocity | r6] The OAuth-token plumbing into MCP is dead
  end-to-end (config key never read, `extract_oauth_tokens` zero callers, false
  docstrings) — cost paid every chat turn and resume. Runtime-verified: no token ever
  reaches a checkpoint [gap2 runtime verifier; whats-fine].
- [01#3 | CONFIRMED | LATENT/cost-perf | r5] `prune_messages` is dead code; history
  replayed unbounded every LLM call; long threads grow ~quadratically in lifetime cost
  and eventually overflow the context window.
- [01#4 | PARTIAL | LATENT/cost-perf | r7] All knowledge entries + annotations injected
  into every system prompt with no budget; the retriever ignores its relevance
  argument.
- [02#3 | CONFIRMED | DEBT/cost-perf | r5 | mixed] Prompt assembly opens serial
  per-table fresh TLS connections on every 60s cache miss; **no Anthropic prompt
  caching anywhere**; per-process caches under 4 uvicorn workers.
- [08#1 | CONFIRMED | LATENT | r2] Checkpointer pool singleton: unsynchronized init
  race; `force_new=True` on any graph-build exception closes the pool under every
  concurrent stream; no borrow-time health check.
- [06#4 | CONFIRMED | DEBT | r2] Stream protocol swallows errors: failures become text
  deltas with `finishReason: "stop"` — failed runs indistinguishable from success to
  the frontend and to downstream effects.
- [06#8 | CONFIRMED | LATENT/security | r1] Thread-ownership check fails **open** on
  `except Exception` — a DB blip lets a foreign thread UUID append into another user's
  conversation.
- [06#9 | PARTIAL | LATENT | r1] No serialization between a resume `ainvoke` and a
  live user turn on the same thread (the one multi-writer surface with no CAS).
- [02#8 | CONFIRMED | LATENT | r3] The 300s stream timeout is only checked between
  events; a stalled call hangs the SSE response (the resume path got `wait_for`; the
  interactive path didn't — fixed-where-it-bit).
- [06#1 | CONFIRMED | BROKEN-NOW | r1] The #190 panic-loop escalation message is never
  streamed live (escalation node emits no chat-model event); the detector couples to
  FastMCP JSON spacing.
- [02#6 | CONFIRMED | DEBT | r3] Prompt instructs `run_materialization` with a
  `pipeline=` parameter the tool no longer accepts; survives only because both
  LangChain and FastMCP silently drop unknown args [07#0 | CONFIRMED].
- [02#4 | CONFIRMED | LATENT/data-loss | r3] Member removal deletes Thread rows but
  never LangGraph checkpoints; checkpoints are never pruned anywhere
  (retention/privacy gap + unbounded growth, see also [10#0]).
- [07#8 | CONFIRMED | DEBT | r3] Dead parallel checkpointer module carries a silent
  MemorySaver-in-production fallback footgun; DEBUG fallback cached for process life.
- [05#9 | CONFIRMED | DEBT | r2 | mixed] Learning lifecycle is inert (confidence never
  auto-adjusts; `times_applied` effectively never increments; prompt implies usage).
- [14#5 | CONFIRMED | BROKEN-NOW | r1] Resume prompt's else-branch tells the agent a
  fully FAILED/CANCELLED materialization "just completed … using the now-loaded data."
- [07#9 | PARTIAL | LATENT | r1] Cascade-FAILED view schemas report a fabricated cause
  ("build failed") and actively wrong recovery advice ("do NOT re-run").
- [07#7 | PARTIAL | LATENT | r1] Checkpointer/thread-list failures return `[]` with
  HTTP 200 — outages render as empty-but-successful UI.
- [06#2 | PARTIAL | DEBT/cost-perf | r3] Rate limiting in per-process LocMemCache
  across 4 workers; provisioned Redis unused.

### 3.4 MCP server, catalog & metadata

- **[01#6 | CONFIRMED | DEBT/security | r5 | mixed] No caller authentication.** Host
  checks only; every tenant-scoped tool trusts `workspace_id` blindly (only
  `run_materialization` verifies membership — and passes for `user_id=""`). Isolation
  is network topology only (production binds 0.0.0.0:8100 on the shared docker
  network; verified not internet-reachable [gap1-infra]).
- **[02#2 | CONFIRMED | BROKEN-NOW | r6] Catalog truth computed 4–6 divergent ways** —
  the prompt advertises transformation assets `list_tables` omits; `get_metadata`
  returns 0 tables for multi-tenant workspaces; this is the #190 panic-loop input
  class. Compounded by ~7 status derivations with user-visible divergence
  (`last_synced_at: null` in UI while the prompt says "Data is loaded and ready")
  [09#6 | CONFIRMED | r3].
- [09#0 | CONFIRMED | BROKEN-NOW/security | r2] Cross-tenant metadata disclosure via
  unqualified `pg_catalog` reads — world-readable regardless of SET ROLE, advertised
  in the system prompt; tenant schema names are customer identifiers; `reltuples`
  leaks row counts. (SET ROLE does block actual cross-tenant *data* reads —
  runtime-verified [gap2].)
- [01#9 | PARTIAL | DEBT | r2] Hardcoded `commcare_sync` pipeline fallback in four
  copy-pasted cascades returns wrong-provider metadata silently.
- [09#7 | CONFIRMED | DEBT | r1] TenantMetadata written per-membership but read with
  three different scopes — annotations appear/disappear per user and per surface.
- [09#8 | CONFIRMED | LATENT | r2] `pipeline_list_tables` fail-closed for sources but
  fail-open for dbt models; a transient error can instruct re-materialization.
- [02#5 | PARTIAL | LATENT | r2] The FutureApp import-time `current_app` binding (cause
  of the fixed janitor bug) survives at three sibling sites with swallowed exceptions.
- [08#0 | CONFIRMED | LATENT | r2] The MCP process has **no** dead-DB-connection
  hygiene — the 2026-06-09 22-hour-outage class was fixed only for worker tasks; after
  the next RDS restart every ORM-touching MCP tool call fails until container restart.
- [00#7 | CONFIRMED | BROKEN-NOW | r7] The `Workspace.tenant` first-tenant compat shim
  silently drives whole features for multi-tenant workspaces: Data Dictionary shows
  only the alphabetically-first tenant's raw tables; refresh, knowledge keying and
  recipe TTL all inherit the wrong scope.

### 3.5 Artifacts & sharing

- **[02#1 | CONFIRMED | BROKEN-NOW/security | r5 | mixed] Sandbox is a no-op.**
  Same-origin + session-authenticated sandbox page executing agent-generated code with
  `allow-scripts allow-same-origin` + CSP `unsafe-eval` + JS-readable CSRF cookie:
  artifact code can issue credentialed state-changing API requests as the viewer.
  Prompt-injected data → artifact → any member who opens it. (Removing
  `allow-same-origin` was runtime-verified to restore isolation [gap2].)
- [00#6 | CONFIRMED | BROKEN-NOW | r11] Multi-tenant live artifacts query the wrong
  schema (§3.1 headline 3).
- [00#8 | CONFIRMED | BROKEN-NOW | r4] `Artifact.conversation_id` never populated —
  shared/public threads always show zero artifacts (resolves contradictions.md C3);
  the tool returns a dead `render_url`.
- [05#1 | CONFIRMED | BROKEN-NOW/security | r7] Recipe `is_shared`/`is_public` enforce
  nothing — "private" recipes visible/runnable/deletable by all members; the agent
  makes a false privacy promise; share-creation UI was removed but public endpoints
  and token-minting model fields remain live.
- [08#6 | CONFIRMED | LATENT/security | r1] Share tokens and OAuth codes transit
  uvicorn/nginx access logs into CloudWatch (30-day retention).
- [09#9 | CONFIRMED | DEBT/cost-perf | r2 | mixed] Full-source copy per artifact
  version, soft-delete never frees rows, live artifacts re-execute all queries
  serially per open with fresh TLS connections each.
- [06#6 | CONFIRMED | LATENT | r2] Widget SDK `setMode()`/theme are no-ops;
  `/labs/scout/` hardcode in the embed OAuth fallback. Also (contradictions.md C4):
  `widget.js` is not routed at all by the primary-prod nginx — the SDK is effectively
  dead on scout.dimagi.com.

### 3.6 Recipes & knowledge

- [00#1 | CONFIRMED | BROKEN-NOW | r11] Recipe runner signature drift — feature 100%
  dead (§1 headline 2). Triple drift: even with the kwarg fixed, initial-state and
  result-extraction shapes have also moved.
- [01#5 | CONFIRMED | LATENT | r4] TableKnowledge keyed by *physical* schema-qualified
  name: every refresh orphans annotations; stale names injected into prompts; in
  multi-tenant workspaces annotations can never match the `ws_*` names the agent sees.
- [05#0 | CONFIRMED | LATENT/data-loss | r2] Data Dictionary autosave silently wipes
  `related_tables` (and other list/dict fields) on every edit — clobber-with-default
  PUT semantics.
- [05#8 | CONFIRMED | LATENT | r2] Knowledge import 500s on malformed input; the
  export→import round trip silently loses duplicate-titled entries.
- [05#7 | CONFIRMED | DEBT/cost-perf | r2] Knowledge list paginates in memory;
  pagination has no UI, so items beyond page 1 are unreachable.
- [06#0 | PARTIAL | LATENT | r2 | mixed] No drift detection for any stored schema
  reference (artifact SQL, knowledge, learnings, recipes); every rename mechanism
  ships without migrating references. The single-tenant design (unqualified SQL +
  search_path) is rename-resistant *by construction* — the failures are the surfaces
  that deviate from it [seam-schema-references, whats-fine].

### 3.7 Provider loaders & upstream contracts

The gap-round upstream-contract verifier read the *provider* codebases (CommCare HQ,
Connect, OCS) and adjudicated Scout's assumptions against them — several round-1
claims were narrowed or corrected:

- [14#3 | CONFIRMED | BROKEN-NOW | r1] **One credential snapshot per run, no mid-run
  refresh**: CommCare's 15-minute OAuth TTL (verified upstream) makes any CommCare-
  OAuth materialization >15 min structurally impossible. Compounding: refresh-failure
  falls back to a known-stale token, and **no 401 anywhere maps to "reconnect your
  account"** [14#4 | CONFIRMED | BROKEN-NOW].
- [12#3 | CONFIRMED | BROKEN-NOW/security | r1] OCS participants sync is **team-wide,
  not chatbot-scoped**: the `chatbot` param Scout sends is documented upstream but
  unimplemented — whole-team rosters + per-chatbot custom data land in a
  single-chatbot tenant schema.
- [12#4 | PARTIAL | LATENT | r1] Scout's CommCare loaders have no retry and no
  429/Retry-After handling at any layer (loader, task, or materializer), so an
  upstream throttle fails the run; retry hardening exists only on Connect [03#6 |
  PARTIAL | r4]. (The finding's upstream framing — that CommCare throttles both APIs
  by design with 429 + Retry-After and floors as low as 10/min — is not verifiable
  from this repository: corehq source is absent and the `@api_throttle` decorator and
  exact limits are unconfirmed here; hence the PARTIAL.)
- [12#5 | CONFIRMED | essential] Correction to a round-1 claim: Case API v2 is keyset
  and cannot silently skip; the offset-skip risk is Forms v0.5 only [09#2 narrowed].
- [12#6 | PARTIAL | BROKEN-NOW/security | r1] Connect's `next` URLs are emitted
  plaintext `http://` in prod — Bearer over a plaintext first hop — *unless* ops
  independently set `FORWARDED_ALLOW_IPS` server-side (unverifiable from the repo;
  BROKEN-NOW is conditional on that unknown). Upstream #1109 was closed *without*
  merging, so no upstream fix landed. The generic next-URL-trust concern [09#1 |
  CONFIRMED | r2] is real but narrower per provider than first claimed.
- [03#7 | PARTIAL | r4] Resumability truth lives in two contradictory registries with
  an unsafe default-True for new Connect sources (re-opens the f26c1a0 duplication
  class on the next source added).
- [09#3 | CONFIRMED | LATENT | r1 | mixed] Mid-rematerialization reads: Connect serves
  silently-partial tables while CommCare/OCS block queries until the 30s timeout —
  two uncommunicated failure modes for the same situation.
- Upstream-shape dead columns / identity bugs: `raw_visits.images` always empty
  [12#8], `raw_sessions.participant_platform` always `''` + sessions→participants join
  conflates identifiers across platforms [12#9], OCS synthetic summary messages make
  Scout's positional `message_id` unstable across syncs [13#0]. OCS loaders use
  page_size 100 (~10–15× excess requests) [13#1]. Connect progress totals are
  permanently None (upstream sends no `count`) [12#7]. Case v2 progress denominators
  above 10k unverified [13#2].
- [02#7 | PARTIAL | LATENT/data-loss | r4] Inbound payloads unguarded: >255-char
  names, missing natural keys, NUMERIC overflow — the 2587158 class has unaudited
  siblings across writers.
- [14#6 | PARTIAL | r1] Connect Retry-After honored uncapped inside the single worker
  thread — an upstream-controlled, uncancellable sleep that blocks the whole platform's
  background work; [14#7 | PARTIAL | r1] an interactive token refresh from the
  settings page can revoke the access token a long run is using.

### 3.8 Transformations / dbt

- [04#3 | CONFIRMED | BROKEN-NOW/security | r2 | mixed] **Arbitrary user SQL as the
  managed-DB superuser** (§1 headline 5): free-text `sql_content` through dbt on the
  `MANAGED_DATABASE_URL` role; cross-tenant reads, DDL, `run_query` jinja. The only
  role-enforced surface (transformations writes require RW) is also the most
  dangerous one — and tenant members can reach it.
- [04#4 | CONFIRMED | BROKEN-NOW | r3] Every *generated* CommCare staging model fails
  at dbt runtime (unqualified `FROM raw_cases` + no profile search_path), triply
  silently.
- [04#5 | CONFIRMED | LATENT | r2] Workspace-scope transformations never run during
  materialization; stale dbt tables survive reloads and re-publish via view schemas.
- [04#6 | CONFIRMED | LATENT | r2] dbt model/alias names from provider metadata have no
  63-byte guard (same class as [00#4]); synchronous `/runs/trigger/` blocks the
  request thread behind a per-process lock.

### 3.9 Frontend & backend↔frontend seam

- [04#7 | CONFIRMED | BROKEN-NOW | r1] Onboarding "Use an API Key" POSTs to the
  endpoint deleted in the #220 rebuild — guaranteed 404 on the critical first-run
  path for every non-OAuth user.
- [04#9 | CONFIRMED | BROKEN-NOW | r2] Artifacts/Recipes pages don't refetch on
  workspace switch → stale cross-workspace data, then 404 actions (the threadId
  incident fix reset only threadId).
- [04#8 | PARTIAL | BROKEN-NOW | r2] BASE_PATH-bypassing root-relative URLs break the
  labs `/scout` deployment: health poll monitors the wrong host's root, sandbox
  iframe and public share pages break.
- [06#3 | CONFIRMED | BROKEN-NOW | r1] Live-stream `toolCallId` mismatch (run UUID vs
  `toolu_…`) kills per-card progress/Stop/failure affordances in live sessions.
- [13#3 | CONFIRMED | BROKEN-NOW | r1] `get_metadata` rich card renders "0 tables" on
  every successful reload (`Array.isArray` over an object map).
- [13#4 | CONFIRMED | BROKEN-NOW | r1] The 2000-char live truncation makes rich tool
  cards fail live for typical payloads but work on reload; error cards ironically work
  live while success cards don't; payload bloat is self-inflicted (indent-2).
- [05#2 | PARTIAL | COSMETIC | r3] The apostrophe→double-quote parse hack; live stream
  sends empty tool input. [13#5 | PARTIAL] Reload silently drops thinking blocks.
  [13#6 | DEBT/velocity; 13#7, 13#8 | COSMETIC/velocity] error-envelope info discarded by cards;
  unmarked 2000-char reload slice; TS-vs-runtime contract inventory.
- [05#3 | CONFIRMED | LATENT | r2] ConnectionsPage post-removal guard compares
  workspace ids to TenantMembership ids — can never fire (old data-model residue).
- [05#4 | PARTIAL | r1] Never-materialized multi-tenant workspace shows an infinite
  fake "Loading data…" spinner. [05#5 | CONFIRMED | r1] WorkspaceDetailPage never
  clears a prior load error.
- [05#6 | CONFIRMED | DEBT/cost-perf | r3 | mixed] Always-on 3s/5s polling with no
  visibility gating; each jobs poll triggers the API-side reconciliation sweep.
- [10#7 | CONFIRMED | COSMETIC | r3] Type lies: TS contracts stricter than runtime;
  prompt-cache key omits schema/knowledge state.

### 3.10 Ops, deploy, infra, observability

- [08#2 | CONFIRMED | DEBT/cost-perf | r4] One worker, concurrency 1: all background
  work platform-wide serializes through a single slot; every merge-deploy kills
  in-flight jobs.
- [08#7 | CONFIRMED | DEBT/velocity | r3 | essential] **No detection layer**: zero
  CloudWatch alarms, static `/health/`, no worker/MCP health surface, crash-looping
  containers deploy green. Process-death incidents emit no operator signal.
- [08#8 | CONFIRMED | BROKEN-NOW | r2] The Django-side agent audit log is suppressed in
  production (INFO under root WARNING) and logs an always-empty `project_id`; MCP
  audit has no actor. [08#9 | CONFIRMED | r2] Schema destruction is silent on success
  — the 2026-06-10 forensic question is *still* unanswerable from logs.
- [08#4 | CONFIRMED | DEBT | r2] Deploys not gated on tests; migration ordering by
  convention; documented pre-deploy hook is `exit 0`.
- [12#2 | CONFIRMED | DEBT | r1] + [10#3 | PARTIAL | r1] CI sets neither DATABASE_URL
  nor MANAGED_DATABASE_URL — every real-DB incident-regression suite (the #227
  truncation tests, the f26c1a0/2587158 pins, role-isolation tests) is skipped under a
  green badge.
- [11#0 | CONFIRMED | LATENT/security | r1 | mixed] One DB, one master-superuser
  credential for both platform and tenant planes. [10#8 | r1] Wide-open egress +
  IMDSv1 → loader SSRF can steal instance-role credentials. [10#9 | r1] CI deploy role
  reads every RDS master password in the account. [11#2 | PARTIAL | r1] RDS/Redis in
  public subnets; SSH 0.0.0.0/0; admin internet-exposed.
- [08#3 | PARTIAL | r1] No `.dockerignore`: the documented manual-deploy path bakes
  developer `.env` into image layers; `/app/.env` is read at runtime as silent default
  for any var Kamal omits.
- [08#5 | CONFIRMED | r1] DEPLOY_ENVIRONMENT heuristic mislabels labs as
  "development"; three hand-maintained Kamal env blocks drift (MCP lacks
  TaskBadger/Langfuse keys yet defers the primary materialization path). [11#1 |
  CONFIRMED | r1] Labs runs on out-of-repo infra — stack-derived findings are
  prod-only facts.
- [11#7 | CONFIRMED | r1] `setup_oauth_apps` composes wrong env-var names — Google/
  GitHub can never bootstrap via the command. [11#8 | PARTIAL | r1]
  `backfill_readonly_roles` aborts on first drift, leaving later schemas role-less
  (fail-closed query outage).
- [10#0 | CONFIRMED | LATENT | r3] Procrastinate job/event tables and checkpoints never
  pruned; retention and janitor correctness are coupled but undesigned.
- [10#1 | PARTIAL | DEBT | r3 | mixed] Fresh TLS connection per MCP operation and per
  artifact query; fresh MCP client + tools/list round trip per chat request. [10#2 |
  r1] Data-dictionary N+1s.

### 3.11 Test architecture

- [10#4 | CONFIRMED | DEBT | r2] The chat↔MCP wire has zero unmocked coverage — the
  highest-churn seam is the one the suite cannot observe; the same masking pattern hid
  the recipes signature drift.
- [12#0 | PARTIAL | r1] The mock audit: tests pin the exact dead seams (FutureApp
  `current_app`, oauth_tokens, FAILED-vs-CANCELLED, the false-fail RUNNING branch is
  never constructed) and affirm the pg_catalog disclosure as contract.
- [10#5 | CONFIRMED | r1] Frontend has no unit-test infrastructure; e2e runs nowhere;
  smoke tests wired to nothing.
- (Plus [12#2]/[10#3] CI-skip findings in §3.10.)

### 3.12 Dead code & residue (rolled up)

[02#0 | PARTIAL | r7] is the umbrella: dead DRF permission classes, the dual
checkpointer module, `project_id` audit residue, `domainSlice` naming stratum, export
501, dead share surface, legacy `data_dictionary` field, vestigial RecipeStep, dead
`execute_async`. [10#6 | PARTIAL | r7] is the cosmetic-drift cluster (Celery
docstrings, removed-model prompt sections, doubled "Knowledge Base" heading, inline
imports). [11#6 | CONFIRMED] AgentLearningAdmin renders escaped literal HTML
(`allow_tags` removed in Django 2.0).

---

## 4. Cross-cutting patterns

These are synthesis-level groupings; every instance cites findings above.

1. **Fixed-where-it-bit.** Incident fixes land at the exact site that burned and
   nowhere else: 63-byte guard on view names but not schema/role/dbt names
   [00#4, 04#6]; TTL touch in `provision()` but not `build_view_schema` [03#2] and
   rewound by `run_pipeline` [04#0]; bounded `ainvoke` on resume but not the
   interactive stream [02#8]; connection hygiene in the worker but not the MCP process
   [08#0]; retry hardening on Connect but not CommCare/OCS [03#6, 12#4]; FutureApp fix
   in tasks.py with three live siblings [02#5]; teardown-state bookkeeping in the
   worker task but not the MCP tool [00#2]; sibling view-schema rebuild on materialize
   but not refresh [00#9]. **This is the single most predictive pattern in the
   codebase: to find a latent bug, find a fix and look for its siblings.**
2. **N implementations of one truth.** ~7 status derivations [09#6], 4–6 table
   catalogs [02#2], 4 pipeline-fallback cascades [01#9], 5 TTL keep-alive variants
   [09#5], 2 resumability registries [03#7], 2 checkpointer modules [07#8], 2 auth
   perimeters [13#9], 3 metadata read-scopes [09#7], 2 cancel semantics [01#1]. Each
   divergence is a user-visible contradiction the agent then has to talk its way
   around (#190 panic loops were exactly this).
3. **The single-tenant→multi-tenant retrofit is unfinished.** First-tenant compat
   shims drive Data Dictionary, refresh, knowledge, recipes [00#7]; artifacts query
   the first tenant's schema [00#6]; chat waives the per-tenant check [01#7];
   `get_metadata` returns 0 tables [02#2]. Multi-tenant is load-bearing in production
   but second-class in roughly every subsystem except the view-schema builder itself.
4. **Dishonest success.** Errors become success at every layer: stream `finishReason:
   "stop"` [06#4], `[]`-with-200 [07#7], "completed" resume prompts for FAILED runs
   [14#5], fabricated view-schema causes [07#9], swallowed login resolution [07#6],
   silent dbt failures [04#4], green CI over skipped regression suites [12#2],
   refresh "success" over destroyed data [00#0]. The project's own honest-progress
   norm is violated most often at the failure path, not the progress bar.
5. **State machines without owners.** Django rows vs physical schemas are reconciled
   by 6+ hand-maintained hooks [09#5]; teardown lacks a CAS [03#0]; a dead state has
   15 readers [03#5]; no janitor owns MaterializationRun [03#9]; ThreadJob creation
   races its own dispatch [01#2]. Where a single owner exists (ThreadJob claim/
   terminal CAS), the review found it *sound* — the pattern works when applied.
6. **Trust-the-caller privilege boundaries.** MCP trusts `workspace_id` [01#6]; the
   sandbox trusts agent code [02#1]; dbt trusts user SQL with superuser [04#3]; admin
   trusts operators with raw state machines [11#3]; content endpoints trust any
   member [00#5]; the LLM-facing tool surface depends on two libraries ignoring
   unknown args [07#0]. Authorization is consistently topology and convention, not
   enforcement.
7. **Contracts have no enforcement mechanism.** Hand-written TS types vs runtime
   [10#7, 13#8], prompt vs tool schema [02#6], docstring vs code (pervasive),
   recipes vs graph signature [00#1], YAML vs hardcoded registry [03#7] — and the
   test suite mocks exactly the seams where these drift [10#4, 12#0]. Drift is not
   detected; it is discovered by users.

---

## 5. Prioritized recommendations

Effort scale: S (<1 day), M (days), L (1–2 weeks), XL (multi-week).

### Now (stop active harm; mostly S/M)

1. **Disable or fix the `/refresh/` path** [00#0, 00#9]. Cheapest correct fix: route
   the button to `materialize_workspace` (which already does in-place reload + sibling
   rebuilds) and delete the `_r`-schema machinery. **S–M. Unblocks: safe Data
   Dictionary refresh; deletes a whole bug family (orphaned annotations [01#5] keying
   churn, two-ACTIVE-rows routing).**
2. **Fix the recipe runner call signature (+ state/result shapes)** [00#1]. **S.
   Unblocks: the entire recipes feature.**
3. **Route multi-tenant artifact query-data through `load_workspace_context`**
   [00#6]. **S. Unblocks: live artifacts for every multi-tenant workspace.**
4. **Drop `allow-same-origin` from the artifact sandbox iframe** (runtime-verified to
   restore isolation) [02#1]. **S. Closes prompt-injection → session takeover.**
5. **Unbind or fix MCP `teardown_schema`** (state updates + sibling handling, or
   remove from the agent toolset) and add the missing state-CAS to the teardown tasks
   [00#2, 03#0]. **M.**
6. **Confine dbt execution**: dedicated low-privilege role + search_path, or gate
   transformation-asset writes behind validation; also fixes generated-model
   search_path so staging models work at all [04#3, 04#4]. **M. Converts the worst
   security finding and a 100%-broken feature in one change.**
7. **Fix the onboarding API-key endpoint** [04#7]. **S. Unblocks: first-run for
   non-OAuth users.**
8. **Three one-liners with outsized blast radius**: fail-closed thread-ownership
   except [06#8]; resume-prompt else-branch honesty [14#5]; reconciler staleness
   measured against the resume job [02#9]. **S each.**
9. **Apply the #227 lesson to all identifier minting**: 63-byte+collision guard on
   schema/role/dbt names; key schema resolution by `(provider, external_id)`
   [00#3, 00#4, 04#6]. **M. Closes the cross-tenant collision class.**

### Next (structural; M/L)

10. **One permission layer.** Enforce roles on the content surface (knowledge,
    recipes, artifacts, jobs, chat-driven destructive tools); delete or adopt
    `permissions.py`; make recipe `is_shared` mean something or remove it; honor
    `archived_at` uniformly [00#5, 05#1, 06#7, 01#7]. **L. Unblocks: inviting
    read-only collaborators safely — currently a false promise.**
11. **One status/catalog module.** Single derivation for materialization/schema
    status and a single table-catalog function used by prompt, tools, and API; write
    MATERIALIZING or delete it; fix `get_schema_status`'s dead shape
    [09#6, 02#2, 03#5, 03#4, 01#9]. **L. Unblocks: agent stops receiving
    contradictory world-state (panic-loop class), UI stops disagreeing with prompts.**
12. **Credential lifetime for long jobs**: mid-run 401-reactive refresh, CommCare
    retry with Retry-After, fail-closed on refresh failure, and a "reconnect your
    account" error mapping [14#3, 14#4, 12#4]. **M–L. Unblocks: CommCare OAuth
    workspaces >15 min of data — currently structurally impossible.**
13. **MCP hardening**: shared-secret auth + membership checks on tenant-scoped tools;
    connection hygiene in the MCP process; a pooled managed-DB connection
    [01#6, 08#0, 10#1]. **M–L.**
14. **Cost/latency floor**: Anthropic prompt caching, history pruning (wire
    `prune_messages` or trim/summarize), knowledge budget, fix OCS page_size
    [02#3, 01#3, 01#4, 13#1]. **M. Direct LLM-bill and latency reduction; prevents
    long-thread context-overflow failures.**
15. **Background-work robustness**: worker concurrency >1 with queues, a
    MaterializationRun janitor wired to procrastinate stalled-job rescue, per-tenant
    advisory lock in the materializer, job-table retention [08#2, 03#9, 03#3, 10#0].
    **L. Unblocks: platform survives one slow tenant and one dead worker.**
16. **Truthful failure**: SSE error chunk type; surface checkpointer failures as
    errors; real `last_error` on cascade-FAILED rows; OCS team-mismatch message
    [06#4, 07#7, 07#9, 07#3]. **M.**
17. **CI/deploy integrity**: set DATABASE_URL/MANAGED_DATABASE_URL in CI (runs the
    incident regressions), gate deploy on CI, `makemigrations --check`,
    `.dockerignore` [12#2, 10#3, 08#4, 08#3]. **M. Unblocks: the green badge meaning
    something.**
18. **Minimum observability**: CloudWatch alarms (job-doing-age, ThreadJob age,
    worker heartbeat), DB-checking health endpoints on all three processes, log
    DROP SCHEMA decisions with inputs, fix the production audit logger
    [08#7, 08#8, 08#9]. **M. Unblocks: detecting the next 22-hour outage in minutes.**

### Guardrails (prevent recurrence)

- **A real contract test on the chat↔MCP wire** (start FastMCP in-process, real
  client, no mocks) and codegen or schema-check for TS API types [10#4, 10#5, 13#8].
  This single guardrail would have caught the recipe breakage, the toolCallId
  mismatch, the `get_metadata` card bug, and the onboarding 404.
- **One identifier helper** (length/collision/sanitization) that every name-minting
  site must use — schema, role, view, dbt, refresh names [pattern 1].
- **Sibling sweep as fix policy**: every incident fix PR must list the grep for
  sibling sites and either fix or tick them off (the review's highest-yield finding
  generator) [pattern 1].
- **Owner modules for shared truths**: the schema dependency graph, status
  derivation, TTL touching — new mutation paths must go through them [09#5].
- **Reference drift detection**: a janitor that validates stored SQL/table references
  (artifacts, knowledge, learnings) against live catalogs and flags rather than
  silently rotting [06#0].
- **Admin lockdown**: read-only registrations for state-machine rows; register the
  operator-needed models; admin behind SSO/IP [11#3, 11#5].
- **Re-run this review's coverage matrix quarterly** (the methodology is repeatable;
  the findings DB diffs against this run).

---

## 6. What's actually fine

Merged from 26 reports' whats-fine sections; kept only entries attested by ≥2
independent reviewers or runtime/adversarially verified.

- **SQL execution defense-in-depth** (generalists 1–3, vertical-mcp, lens-security):
  sqlglot validation (single statement, SELECT-only, function denylist, LIMIT
  injection) **plus** `SET ROLE <schema>_ro` + statement_timeout. Runtime-verified:
  the readonly role does block cross-tenant data SELECT [gap2]. The pg_catalog
  *metadata* gap [09#0] is the one hole.
- **ThreadJob/resume CAS discipline** (≥6 reports): claim CAS excluding RUNNING,
  terminal CAS scoped to RUNNING with re-read on miss, janitor flips CAS-scoped,
  `_procrastinate_job_status` None-means-don't-touch. The 19-commit fix chain
  converged somewhere genuinely sound.
- **Materializer phase transitions** (≥5 reports): every DISCOVERING→…→COMPLETED flip
  is a conditional UPDATE preserving external CANCELLED; pre-loop failures stamp
  terminal states; per-source isolation and PARTIAL semantics are honest.
- **`build_view_schema` post-#227** (≥6 reports): bounded deterministic prefixes,
  byte-accurate pre-DDL length and collision checks, idempotent rebuild, `last_error`
  persistence — the model the other identifier paths should copy.
- **The #229 failure-surfacing chain** (≥4 reports): vs.state=FAILED + last_error →
  resume prompt → get_schema_status → ThreadJob, wired end-to-end.
- **Chat entry hygiene** (generalists, vertical-chat, seam-chat): thread-ownership
  404-not-403, cross-workspace rejection, dangling-tool-call repair on both paths,
  CSRF + rate limiting on a raw async view (modulo [06#8]'s broad except).
- **`merge_users` mechanics** (4 reports): transactional, per-relation conflict
  policy, `_meta`-driven long-tail FK repoint, dry-run (modulo the privilege-flag
  propagation [11#4]).
- **Connect loader engineering** (3 reports + upstream verifier): bounded retry,
  typed errors with sentry-trace, the visits resume watermark verified field-for-field
  against upstream keyset semantics; `connect_sync.yml` resumable flags accurate.
- **Catalog reconciliation #185/#187** (4 reports): completed-sources ∩
  information_schema, in_progress hidden, fail-closed for sources.
- **Async discipline** (async-lens AST scan + generalists): zero sync ORM calls in 142
  async functions; `sync_to_async` policy honored; `aresolve_credential` correctly
  used at both task call sites.
- **Worker connection-hygiene decorator** (4 reports): correct for what it covers,
  test-enforced over every task, tracked for upstream removal — exemplary stopgap
  handling. (Its gap is process scope [08#0], not its logic.)
- **Cancel ordering** (3 reports): DB flip before procrastinate abort, single funnel,
  matches the worker checkpoint.
- **Secrets pipeline & token encryption** (ops lens + accounts vertical + gap1-infra):
  Secrets Manager → Kamal, OIDC CI auth, no secret values in logs or task args,
  Fernet encryption of social tokens at rest; checkpointer verified token-free at
  runtime [gap2].
- **Share-token mechanics** (2 reports): `token_urlsafe(32)`, regenerate-on-enable,
  null-on-disable; public views check the flags (the *enforcement* gaps are recipes'
  flags [05#1], not the token scheme).
- **Workspace management guards** (3 reports): last-manager protection, tenant-add
  requires requester's own membership, member-add requires shared tenant, uniform
  membership scoping at every workspace entry point.
- **Infra basics** (gap1-infra): only the frontend is internet-exposed; RDS encrypted,
  non-public, managed password; OIDC deploy trust pinned to the repo; EC2 instance
  role least-privilege.
- **Progress honesty norm** (2 reports): real denominators, sessions-denominated OCS
  messages, indeterminate-on-resume — the norm holds where implemented (violations
  are the spinner/status findings, not the progress bars).

---

## 7. Coverage appendix

**Process**: 43 reviewers across three rounds (round 1: 3 generalists, 10 verticals,
10 lenses, 5 seams, 3 journeys, 1 git historian + 3 cartography-proposed extras;
gap round 1: 5 targeted; gap round 2: 3 targeted, one of them a runtime verifier that
executed code against a live stack). 148 clustered findings adjudicated by independent
adversarial verifiers (2 verifiers for S1/security); a dedicated contradiction pass
resolved 8 cross-report disagreements against the code (contradictions.md). The
gap-round upstream verifier read the CommCare HQ / Connect / OCS source upstream and
corrected two round-1 claims ([12#5], parts of [12#6]).

**Depth (from the merged coverage logs):** 477 files deep-read at least once; 338
more skim-only. The hot spine was read to extreme redundancy: `mcp_server/context.py`
×23, `mcp_client.py` ×16, `query.py` ×15, `schema_manager.py` ×14, `tasks.py` and
`models.py` ×13, `server.py`/`chat/views.py`/`api/views.py` ×12, `graph/base.py` ×11.

| Area | Depth | Confidence in this report's claims |
|---|---|---|
| workspaces tasks/schema lifecycle | 107 files touched, 177 deep-reads | **High** — most-replicated findings live here |
| mcp_server (incl. loaders) | 110 files, 185 deep-reads; loaders ×7 max | **High**; loader claims additionally verified against upstream source where available (CommCare HQ throttle internals were not — see [12#4]) |
| users/auth/allauth | 61 files, 110 deep-reads + dedicated gap-round vertical | **High** |
| chat/agents | 83 files, 161 deep-reads | **High** |
| artifacts / recipes / knowledge | 78 files, 92 deep-reads | **Medium-high** (max redundancy 6–9) |
| transformations | 20 files, 24 deep-reads | **Medium** — one vertical + one lens; findings verified but less replication |
| frontend | 129 files touched, but shallow (max redundancy 5; 123 total deep-reads over 13k LOC) | **Medium** — seam/vertical coverage of the store, router, chat, workspace pages; many leaf components only skimmed |
| config / deploy / infra | 43 files, 66 deep-reads + dedicated infra reviewer | **Medium-high**; labs infra is out-of-repo and explicitly NOT verified [11#1] |
| tests (as architecture) | 51 files touched, mostly skim + dedicated mock-audit | **Medium** |

**Explicitly NOT reviewed / low confidence:**
- ~235 repo files never opened by any reviewer — 115 are test files and most of the
  rest are `__init__.py`/`apps.py`/barrel `index.ts` files, but ~30 substantive
  frontend leaf components (e.g. `SlashCommandMenu.tsx`, `CreateWorkspaceModal.tsx`,
  `SearchFilterBar.tsx`, `threadStorage.ts`, `brandIcons.tsx`) were never deep-read.
- The labs (connectlabs) runtime: out-of-repo ECS infra; all stack-level findings are
  production-stack facts only [11#1].
- Live provider behavior beyond source-reading: upstream claims are verified against
  provider *source*, not against live API traffic (except where the runtime verifier
  ran); [13#2] (ES total-hits cap) remains explicitly unresolved.
- Actual production log/telemetry content ([08#6] is verified from config, not from
  CloudWatch output).
- LLM behavioral quality (prompt effectiveness, answer quality) — out of scope; only
  prompt↔code contract drift was reviewed.

**Replication signal**: 23 findings were independently found by ≥5 reviewers (14 by
≥7); all survived verification. The two REFUTED findings were both single-mechanism inferences
that adversarial trace overturned — the verification layer earned its cost.

### Appendix B — Refuted findings (retained for the record, excluded from the body)

- [04#2] "Connect resumable writers can duplicate on stale-cursor replay" — REFUTED:
  the current DROP+reload behavior for non-cleanly-resumed runs prevents the replay
  path; the finding's residual value survives as the [03#7] registry-default warning.
- [07#5] "auto_create_workspace_on_membership performs three writes outside a
  transaction" — REFUTED on the claimed consequence; the idempotency guard makes the
  gap a non-issue in practice.
