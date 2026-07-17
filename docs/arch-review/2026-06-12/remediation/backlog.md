# Scout arch-review remediation backlog

Generated from the 2026-06-12 review (repo HEAD 35e4230). 148 findings -> 34 issues (+1 design epics, 2 policies).

Do not hand-edit -- regenerate with `uv run build_issue_map.py`.

## Wave 0 -- make the safety net real

### CI runs the real-DB regression suites and deploys gate on green  `ci-deploy-integrity` [M] [BROKEN-NOW] (4 findings)

CI sets neither DATABASE_URL nor MANAGED_DATABASE_URL, so every real-DB incident-regression suite is silently skipped under a green badge; deploys are not gated on tests. Until this lands, no fix below is actually verified by the suite. Also: makemigrations --check, .dockerignore.

- [ ] `12#2` DEBT/velocity r1 -- CI sets neither DATABASE_URL nor MANAGED_DATABASE_URL, so every real-DB incident-regression suite is skipped under a green badge
- [ ] `10#3` BROKEN-NOW/correctness r1 -- CI never runs the 2026-06-10 incident regression tests or any real-DB writer/role-isolation tests (module-level MANAGED_DATABASE_URL skip)
- [ ] `08#4` DEBT/correctness r2 -- Deploys not gated on tests; migration ordering by convention; labs migrations opt-in default-off; version-skew windows
- [ ] `08#3` DEBT/security r1 -- No .dockerignore: documented manual-deploy path bakes developer .env/.env.deploy into the production image; base.py loads /app/.env at runtime

### Real (unmocked) chat<->MCP contract test + frontend test infra  `chat-mcp-contract-test` [M] (5 findings)

Stand up FastMCP in-process with a real client, no mocks, on the highest-churn seam. Would have caught the recipe breakage, toolCallId mismatch, get_metadata card bug, and onboarding 404 as a CLASS. Includes frontend unit-test infra and retiring the mocks that pin dead seams.

- [ ] `10#4` DEBT/correctness r2 -- Chat-to-MCP wire has zero unmocked coverage; tests mock the exact seams that hid production bugs
- [ ] `10#5` DEBT/velocity r1 -- Frontend has no unit-test infrastructure; e2e runs nowhere; post-deploy smoke tests exist but are wired to nothing
- [ ] `12#0` DEBT/velocity r1 -- Test suite is structurally blind to the known incident classes: mocks pin the exact dead seams (FutureApp current_app, oauth_tokens, FAILED-vs-CANCELLED, stale-cursor selection, RUNNING false-failure), affirms the pg_catalog disclosure as contract, and tests no role enforcement
- [ ] `02#6` DEBT/correctness r3 -- Prompt instructs run_materialization with a pipeline= parameter the tool no longer accepts (single-tenant branch); recurring prompt-vs-contract drift
- [ ] `07#0` DEBT/velocity r1 -- Context-injection contract relies on both LangChain and FastMCP skipping arg validation

## Wave 1 -- stop active harm (BROKEN-NOW data-loss + security)

### One identifier helper: 63-byte + collision guard on every minted name  `identifier-minting-helper` [M] (3 findings)

The #227 fix was applied to view names only. Build ONE helper (length/collision/sanitization, keyed by (provider, external_id)) that schema, role, refresh, dbt model/alias names all route through. Closes the cross-tenant collision class -- same bug family as the 2026-06-10 incident.

- [ ] `00#3` LATENT/security r7 -- Tenant identity keyed by bare external_id: cross-provider / punctuation collision routes one tenant into another's schema
- [ ] `00#4` LATENT/security r8 -- Tenant schema names and derived role/refresh names have no 63-byte guard (truncation fix applied to view names only)
- [ ] `04#6` LATENT/correctness r2 -- dbt model names and column aliases from CommCare metadata have no 63-byte guard; synchronous /runs/trigger/ blocks the request thread with a per-process lock

### Data Dictionary refresh destroys the data it just loaded  `refresh-data-loss` [M] [BROKEN-NOW] (2 findings)

ACTIVELY DESTROYS PROD DATA ON EVERY CLICK. Cheapest correct fix: route the button to materialize_workspace (in-place reload + sibling rebuilds) and delete the _r-schema machinery. Put this FIRST in wave 1.

- [ ] `00#0` BROKEN-NOW/data-loss r14 -- Refresh loads fresh data into the old schema then destroys it (legacy /refresh/ path)
- [ ] `00#9` LATENT/correctness r1 -- Refresh path never rebuilds dependent multi-tenant view schemas; teardown comment is false on that path

### MCP teardown_schema updates Django state; add CAS at the drop site  `mcp-teardown-and-state-cas` [M] [BROKEN-NOW] (2 findings)

The agent-exposed teardown tool drops physical schemas but never updates Django state and cascade-kills sibling workspaces; the queued teardown task drops resurrected rows with no state CAS. Fix or unbind the tool; add the CAS.

- [ ] `00#2` BROKEN-NOW/data-loss r7 -- MCP teardown_schema tool drops physical schemas but never updates any Django state, and destroys sibling workspaces' shared tenant schemas
- [ ] `03#0` LATENT/data-loss r2 -- provision() resurrects TEARDOWN/EXPIRED rows but the already-queued teardown task drops them unconditionally (no state CAS)

### Recipe runner signature drift -- feature 100% dead since March  `recipe-runner-fix` [S] [BROKEN-NOW] (1 finding)

RecipeRunner calls build_agent_graph with a removed kwarg -> TypeError -> 500 -> RecipeRun stranded RUNNING forever. Triple drift (kwarg + initial-state + result-extraction). Restores the feature to working-as-designed; the redesign question is separate (see content-satellite-redesign).

- [ ] `00#1` BROKEN-NOW/correctness r11 -- Recipe execution 100% broken: RecipeRunner calls build_agent_graph with removed tenant_membership signature

### Artifact sandbox is a no-op (allow-scripts + allow-same-origin)  `artifact-sandbox-isolation` [S] [BROKEN-NOW] (1 finding)

Drop allow-same-origin from the artifact iframe (runtime-verified to restore isolation). Closes prompt-injection -> session takeover: agent-generated code can currently issue credentialed state-changing requests as the viewer.

- [ ] `02#1` BROKEN-NOW/security r5 -- Artifact sandbox iframe: allow-scripts + allow-same-origin neutralizes the sandbox; agent-generated code runs same-origin with full user API authority

### Multi-tenant live artifacts query the wrong schema / show zero artifacts  `artifact-multitenant-render` [S] [BROKEN-NOW] (2 findings)

Route artifact query-data through load_workspace_context (view schema, not first tenant's schema); populate Artifact.conversation_id so shared/public threads stop showing zero artifacts and the dead render_url is fixed.

- [ ] `00#6` BROKEN-NOW/correctness r11 -- Live-query artifacts in multi-tenant workspaces execute against the first tenant's schema, not the view schema
- [ ] `00#8` BROKEN-NOW/correctness r4 -- Artifact.conversation_id never populated; shared/public threads always show zero artifacts; tool returns dead render_url

### dbt runs arbitrary user SQL as managed-DB superuser; generated models fail  `dbt-transformations` [M] [BROKEN-NOW] (3 findings)

Confine dbt: dedicated low-privilege role + search_path (or gate transform writes behind validation). Same change fixes the generated CommCare staging models that fail silently, and the workspace-scope transforms that never run.

- [ ] `04#3` BROKEN-NOW/security r2 -- Transform assets execute arbitrary user SQL via dbt as the managed-DB superuser — no validation, no SET ROLE downgrade, no schema confinement
- [ ] `04#4` BROKEN-NOW/correctness r3 -- dbt staging models can't resolve raw tables: generated SQL is unqualified and the dbt profile sets no search_path — every generated CommCare staging model fails silently
- [ ] `04#5` LATENT/correctness r2 -- Workspace-scope transformations never run during materialization; stale dbt tables presented as fresh; orphaned system assets keep executing

### Onboarding 'Use an API Key' POSTs to a deleted endpoint (guaranteed 404)  `onboarding-apikey-404` [S] [BROKEN-NOW] (1 finding)

First-run path for every non-OAuth user 404s. Re-point to the live endpoint.

- [ ] `04#7` BROKEN-NOW/correctness r1 -- Onboarding 'Use an API Key' form POSTs to the deleted /api/auth/tenant-credentials/ endpoint — guaranteed 404 on the critical first-run path

### Three one-liners with outsized blast radius  `high-blast-one-liners` [S] [BROKEN-NOW] (3 findings)

Fail-CLOSED thread-ownership except (currently fails open -> foreign thread append); resume-prompt else-branch honesty (stops telling the agent a FAILED run 'just completed'); reconciler staleness measured against the resume job (stops falsely failing healthy long materializations).

- [ ] `06#8` LATENT/security r1 -- Chat thread-ownership check fails open on broad except
- [ ] `14#5` BROKEN-NOW/correctness r1 -- Resume prompt else-branch tells the agent the materialization 'just completed ... using the now-loaded data' for fully FAILED and CANCELLED runs
- [ ] `02#9` BROKEN-NOW/correctness r2 -- Stale-job reconciler falsely fails healthy in-flight resumes for materializations whose dispatch-to-resume exceeds 10 minutes

### Cross-tenant metadata disclosure via unqualified pg_catalog reads  `mcp-metadata-disclosure` [M] [BROKEN-NOW] (1 finding)

pg_catalog is world-readable regardless of SET ROLE and is advertised in the system prompt; tenant schema names are customer identifiers and reltuples leaks row counts. (SET ROLE does block actual cross-tenant DATA reads.)

- [ ] `09#0` BROKEN-NOW/security r2 -- Cross-tenant metadata disclosure via unqualified pg_catalog reads, advertised in the system prompt; SQL validator gaps where SET ROLE is the only defense

### OCS participants sync is team-wide, not chatbot-scoped  `ocs-team-scope` [M] [BROKEN-NOW] (2 findings)

The 'chatbot' param Scout sends is documented upstream but unimplemented, so whole-team rosters land in a single-chatbot tenant schema. Plus: team-mismatch currently surfaces as a generic 'No credential configured'.

- [ ] `12#3` BROKEN-NOW/security r1 -- OCS participants sync is team-wide, not chatbot-scoped: Scout sends a 'chatbot' param upstream silently ignores, loading whole-team rosters + per-chatbot custom data into a single-chatbot tenant schema
- [ ] `07#3` DEBT/correctness r3 -- Multi-team OCS team-mismatch surfaces as generic 'No credential configured', indistinguishable from no connection

### Live/reload tool-output rich cards broken (toolCallId, 0-tables, truncation)  `frontend-tool-cards` [M] [BROKEN-NOW] (8 findings)

toolCallId mismatch kills per-card progress/Stop live; get_metadata renders '0 tables' on reload (Array.isArray over a map); 2000-char live truncation breaks success cards live; thinking blocks dropped on reload; error-envelope info discarded; the apostrophe->double-quote parse hack.

- [ ] `06#3` BROKEN-NOW/correctness r1 -- Live-stream toolCallId mismatch kills per-card materialization progress/Stop/failure affordances during live sessions
- [ ] `13#3` BROKEN-NOW/correctness r1 -- get_metadata rich card renders '0 tables' on every successful reload: Array.isArray over an object map
- [ ] `13#4` BROKEN-NOW/correctness r1 -- Per-tool live/reload split refines the truncation finding: query/describe/list rich cards fail live for typical payloads but work on reload; self-inflicted indent-2 whitespace bloat; live tool cards appear only at tool-end with isLoading states unreachable
- [ ] `05#2` COSMETIC/correctness r3 -- Tool-output rich rendering depends on a global apostrophe->double-quote replace; live stream sends empty tool input and 2000-char-truncated unparseable output
- [ ] `13#5` LATENT/correctness r1 -- Thread reload silently drops reasoning/thinking parts: message_converter emits only text and tool parts
- [ ] `13#6` DEBT/velocity r1 -- Tool error-envelope information discarded by rich cards and get_schema_status envelope self-inconsistency: error fields omitted from TS types, timing_ms missing, FAILED variant nests error string inside data
- [ ] `13#7` COSMETIC/velocity r1 -- Reload fallback display silently slices output to 2000 chars with no marker despite having the full string client-side
- [ ] `13#8` COSMETIC/velocity r1 -- TS-vs-runtime contract inventory: note string|null, JSONB cells render '[object Object]', warnings/project_id untyped, apostrophe-replace branch vestigial, output-error state unproducible

### Pages don't refetch / clear state on workspace switch  `frontend-workspace-switch` [S] [BROKEN-NOW] (3 findings)

Artifacts/Recipes show stale cross-workspace data then 404 (threadId fix reset only threadId); ConnectionsPage guard compares the wrong ids (never fires); WorkspaceDetailPage never clears a prior load error.

- [ ] `04#9` BROKEN-NOW/correctness r2 -- Artifacts and Recipes pages don't refetch on workspace switch: stale cross-workspace data then 404 actions
- [ ] `05#3` LATENT/velocity r2 -- ConnectionsPage post-removal workspace-switch guard compares workspace ids to TenantMembership ids — can never fire
- [ ] `05#5` LATENT/correctness r1 -- WorkspaceDetailPage never clears a prior load error: subsequent successful loads still render the error screen

### BASE_PATH-bypassing URLs break the labs /scout deployment + widget SDK  `base-path-and-labs` [M] [BROKEN-NOW] (4 findings)

Root-relative URLs break health poll, sandbox iframe, public share pages on labs; widget setMode/theme are no-ops and widget.js isn't routed; DEPLOY_ENVIRONMENT mislabels labs as development. (Labs infra is out-of-repo.)

- [ ] `04#8` BROKEN-NOW/correctness r2 -- BASE_PATH-bypassing root-relative URLs break labs /scout deployment: health poll, artifact sandbox iframe, and public share pages
- [ ] `06#6` LATENT/correctness r2 -- Widget SDK setMode()/theme are no-ops; host-specific '/labs/scout/' hardcode in embed OAuth fallback
- [ ] `11#1` DEBT/velocity r1 -- CloudFormation stack describes production only; labs runs on out-of-repo ECS Fargate infra (deploy-target drift)
- [ ] `08#5` LATENT/velocity r1 -- DEPLOY_ENVIRONMENT heuristic mislabels connectlabs as 'development' for Sentry/Task Badger; three hand-maintained env blocks drift (MCP lacks Task Badger/Langfuse keys)

### [POLICY] Sibling-sweep as fix policy on every incident-fix PR  `sibling-sweep-policy` [S]

Pattern 1 ('fixed-where-it-bit') is the single most predictive finding generator: every fix PR must list the grep for sibling sites and either fix or explicitly tick them off.


References: `identifier-minting-helper`

## Wave 2 -- structural consolidations

### Finish the single-tenant -> multi-tenant retrofit (first-tenant shim)  `multitenant-retrofit-shim` [L] [**DESIGN-GATED**, BROKEN-NOW] (3 findings)

DESIGN-GATED (what SHOULD a multi-tenant workspace show?). The first-tenant compat shim silently drives Data Dictionary, refresh, knowledge, recipe TTL; never-materialized multi-tenant workspaces spin forever; zero-tenant workspaces dead-end in chat.

- [ ] `00#7` BROKEN-NOW/correctness r7 -- Single-tenant 'first tenant' compat shim silently drives whole features for multi-tenant workspaces (Data Dictionary, refresh, knowledge, recipe TTL)
- [ ] `05#4` LATENT/correctness r1 -- Never-materialized multi-tenant workspace reports 'provisioning' forever: perpetual fake 'Loading data...' spinner
- [ ] `06#5` LATENT/correctness r2 -- Chat dead-end in tenant-less workspaces: UI allows zero-tenant workspaces, backend 403, frontend generic error

### One permission layer enforced on the content surface  `permission-layer` [L] [**DESIGN-GATED**, BROKEN-NOW] (5 findings)

DESIGN-GATED (what should READ/RW/MANAGE actually permit? should recipe is_shared/is_public exist at all?). Today DRF permission classes have zero importers; READ members mutate artifacts/knowledge/recipes and drive destructive agent tools. Honor archived_at uniformly; fix dead role tests.

- [ ] `00#5` BROKEN-NOW/security r8 -- WorkspaceRole unenforced: DRF permission classes are dead code; READ members can mutate knowledge/recipes/artifacts and drive destructive agent tools via chat
- [ ] `05#1` BROKEN-NOW/security r7 -- Recipe is_shared / is_public privacy toggles enforce nothing; 'private' recipes/runs visible and runnable by all members; share-creation UI gone but public endpoints live
- [ ] `06#7` BROKEN-NOW/security r3 -- Disconnect/disappear paths handle archived_at inconsistently across 5+ authz sites; tenant resolution is additive-only so upstream-revoked access stays readable
- [ ] `01#7` DEBT/security r5 -- Tenant-membership requirement asymmetric: multi-tenant chat waives the TenantMembership check single-tenant chat enforces
- [ ] `12#1` DEBT/security r1 -- test_workspace_permissions.py has zero role assertions and dead role fixtures; permissions.py has zero importers anywhere

### One status/catalog module (single source of world-state truth)  `status-catalog-module` [L] [**DESIGN-GATED**, BROKEN-NOW] (7 findings)

DESIGN-GATED (define the canonical shape). Status/catalog derived ~7 ways with user-visible divergence -> the #190 panic-loop input class. Single derivation for status; single table-catalog used by prompt+tools+API; write MATERIALIZING or delete it (15 readers, 0 writers); fix get_schema_status's dead shape; reconcile the 3 metadata read-scopes; fix fail-open dbt catalog.

- [ ] `09#6` BROKEN-NOW/correctness r3 -- Status/catalog derivation implemented ~7 ways with concrete user-visible divergence (last_synced_at COMPLETED-only vs prompt/get_schema_status COMPLETED|PARTIAL); 'usable' view-schema predicate diverges during rebuild
- [ ] `02#2` BROKEN-NOW/correctness r6 -- Multiple divergent table-catalog implementations: system prompt advertises terminal/transformation assets that list_tables omits; get_metadata returns 0 tables for multi-tenant; get_schema_status reads a dead result shape
- [ ] `03#4` DEBT/correctness r3 -- get_schema_status reads an extinct result shape: single-tenant 'tables' always [] after a successful run
- [ ] `03#5` DEBT/correctness r4 -- SchemaState.MATERIALIZING is never written by production code: ~15 readers branch on a dead state; single-tenant in-progress guard unreachable
- [ ] `01#9` DEBT/velocity r2 -- Hardcoded 'commcare_sync' pipeline fallback in four places returns wrong-provider metadata silently; cascade duplicated 3-4x
- [ ] `09#8` LATENT/cost-perf r2 -- pipeline_list_tables is fail-closed for sources but fail-open for dbt models; transient DB error yields a phantom-table or empty catalog that instructs re-materialization
- [ ] `09#7` DEBT/correctness r1 -- TenantMetadata is per-membership but read with three different scopes: column annotations appear/disappear per user and per surface

### Credential lifetime for long jobs (CommCare 15-min OAuth TTL)  `credential-lifetime-long-jobs` [L] [BROKEN-NOW] (7 findings)

Any CommCare-OAuth materialization >15 min is structurally impossible today: one credential snapshot per run, no mid-run/401 refresh, stale-token fallback, no 'reconnect your account' mapping. Plus CommCare/OCS retry hardening, uncancellable Retry-After sleep, and refresh-revokes-running-token race.

- [ ] `14#3` BROKEN-NOW/correctness r1 -- One credential snapshot per materialization run with no mid-run/401 refresh: CommCare's 15-min OAuth TTL makes long CommCare OAuth syncs structurally impossible
- [ ] `14#4` BROKEN-NOW/correctness r1 -- Refresh-failure falls back to a known-stale token and no 401 anywhere maps to re-authentication guidance; reactive-refresh path is dead code with false docstring
- [ ] `12#4` LATENT/correctness r1 -- CommCare HQ actively rate-limits both APIs (429 + Retry-After by design); Scout's CommCare loaders have no retry and fail the run — raises the known Connect-only-hardening finding to expected-in-production for CommCare
- [ ] `14#6` LATENT/cost-perf r1 -- Connect Retry-After honored uncapped inside the single worker thread; '~14s worst case' comment false and the sleep is uncancellable
- [ ] `14#7` LATENT/correctness r1 -- Concurrent interactive token refresh (providers poll) can revoke the access token a long-running load is using mid-run
- [ ] `03#6` DEBT/correctness r4 -- Retry/error-shape hardening applied only to Connect loaders; OCS/CommCare swallow malformed responses and have no retry
- [ ] `09#3` LATENT/correctness r1 -- Mid-rematerialization reads: Connect serves silently-partial tables while CommCare/OCS block queries until 30s timeout

### MCP server hardening: caller auth, connection hygiene, pooling  `mcp-hardening` [M] (4 findings)

No caller authentication: tenant-scoped tools trust workspace_id blindly. Add shared-secret auth + membership checks; add dead-DB-connection hygiene (the 22-hour-outage class, fixed only for the worker); pool the managed-DB connection; delete the dead OAuth-token plumbing paid for every chat turn.

- [ ] `01#6` DEBT/security r5 -- MCP HTTP server has no caller authentication; teardown_schema/query trust workspace_id blindly; isolation is network topology only
- [ ] `08#0` LATENT/correctness r2 -- MCP server process has no Django dead-DB-connection hygiene; ORM on asyncio.to_thread pool threads in refresh/view-schema builds also unguarded
- [ ] `10#1` DEBT/cost-perf r3 -- Connection-per-operation cost: fresh TLS psycopg connection per MCP query/describe/list and per artifact query; fresh MCP client + tools/list HTTP round trip per chat request
- [ ] `01#0` DEBT/velocity r6 -- OAuth-token plumbing into MCP is dead end-to-end with false docstrings; cost paid every chat turn and resume

### Cost/latency floor: prompt caching, history + knowledge budgets, polling  `cost-latency-floor` [M] (10 findings)

No Anthropic prompt caching anywhere; prune_messages is dead (history replayed unbounded, ~quadratic lifetime cost + eventual context overflow); knowledge injected with no budget; serial per-table TLS connections per cache miss; always-on polling; in-memory knowledge pagination; per-version artifact copies; OCS page_size; uncached me_view re-hitting all providers; DD N+1s.

- [ ] `02#3` DEBT/cost-perf r5 -- System-prompt assembly opens serial per-table fresh TLS DB connections every 60s cache miss; no Anthropic prompt caching; per-process caches under 4 workers
- [ ] `01#3` LATENT/cost-perf r5 -- prune_messages is dead code: conversation history replayed unbounded every LLM call, no trimming/summarization/tool-result compaction
- [ ] `01#4` LATENT/cost-perf r7 -- Knowledge context injected into every system prompt with no size cap; retriever ignores its relevance argument
- [ ] `13#1` DEBT/cost-perf r1 -- OCS loaders use upstream default page_size=100 — ~10-15x more list requests than necessary; ocs_participants docstring says 'no count today' but upstream now provides first-page count
- [ ] `06#2` DEBT/cost-perf r3 -- Chat rate limiting uses per-process LocMemCache; ineffective across uvicorn workers and Redis is provisioned but unused
- [ ] `05#6` DEBT/cost-perf r3 -- Always-on polling (jobs 3s + health 5s) with no visibility gating; each jobs poll triggers API-side janitor reconciliation (~5 DB queries)
- [ ] `05#7` DEBT/cost-perf r2 -- Knowledge list endpoint paginates in memory over all rows; pagination has no UI so items beyond page 1 are unreachable
- [ ] `09#9` DEBT/cost-perf r2 -- Artifact versioning copies full code per update and soft-delete never frees rows; live artifacts re-execute all source queries serially per open with no caching
- [ ] `10#2` DEBT/cost-perf r1 -- Data dictionary view: per-table TableKnowledge N+1, two fresh managed-DB connections per request, and async_to_sync in a sync DRF view
- [ ] `07#4` LATENT/cost-perf r1 -- me_view recomputes onboarding uncached on every /me poll, re-hitting all three provider APIs and flapping onboarding_complete for token-but-no-tenant users

### Background-work robustness: concurrency, janitors, per-tenant locking  `background-work-robustness` [L] [BROKEN-NOW] (16 findings)

One worker at concurrency 1 serializes all background work; no janitor owns MaterializationRun after worker death (zombie 'doing' jobs); no per-tenant mutual exclusion; ThreadJob races its own dispatch; cancel semantics diverge; FutureApp current_app siblings; TTL rewind; checkpointer pool race; resume vs live turn unserialized; purge orphans; the dependency graph has no owner; stream 300s timeout only checked between events; job/checkpoint tables unpruned.

- [ ] `08#2` DEBT/cost-perf r4 -- Single worker process at concurrency 1: all background work serialized platform-wide; worker deploy on every merge kills in-flight jobs
- [ ] `03#9` BROKEN-NOW/correctness r4 -- No janitor reconciles MaterializationRun rows stuck in ACTIVE states after hard worker death; procrastinate stalled-job rescue never wired
- [ ] `03#3` LATENT/data-loss r5 -- No per-tenant mutual exclusion for materialization: thread-scoped guard permits concurrent DROP/CREATE/INSERT on a shared tenant schema
- [ ] `10#0` LATENT/cost-perf r3 -- Procrastinate job/event tables and LangGraph checkpoints never pruned; queue-table growth couples to janitor correctness
- [ ] `01#2` DEBT/correctness r7 -- ThreadJob created after defer_async (dispatch/creation race); resume lifecycle is a long fix-chain held together by hedge + janitor
- [ ] `01#1` LATENT/correctness r9 -- Two/three materialization-cancel mechanisms with diverging semantics; MCP cancel_materialization writes FAILED not CANCELLED and never stops the load
- [ ] `07#1` LATENT/correctness r2 -- Cross-workspace cancellation: materialization_cancel_view selects orphan runs by shared tenant, cancels the same user's run in a sibling workspace
- [ ] `02#5` LATENT/correctness r2 -- FutureApp import-time current_app binding (cause of the fixed janitor bug) survives at three sibling sites with swallowed exceptions
- [ ] `04#0` DEBT/correctness r2 -- run_pipeline completion saves stale provision-time last_accessed_at, rewinding the TTL clock by the run duration
- [ ] `03#1` LATENT/correctness r3 -- Partial-failure or cancel during multi-tenant rematerialization leaves the workspace's own view schema cascade-dropped but ACTIVE
- [ ] `03#2` LATENT/correctness r2 -- build_view_schema reactivates EXPIRED view-schema rows without resetting last_accessed_at (incident-b class, view-schema edition)
- [ ] `08#1` LATENT/correctness r2 -- Checkpointer pool singleton: unsynchronized init race; force_new closes the shared pool under concurrent streams; no borrow-time health check
- [ ] `06#9` LATENT/correctness r1 -- No serialization between a resume ainvoke and a live user chat turn on the same LangGraph thread
- [ ] `09#4` DEBT/correctness r1 -- purge_synced_data orphans WorkspaceViewSchema rows and ws_* physical schemas
- [ ] `09#5` DEBT/velocity r3 -- Dependency graph between tenant schemas and view schemas has no owner: reconciled by 6+ hand-maintained hooks, four live mutation paths lack required hooks
- [ ] `02#8` LATENT/cost-perf r3 -- Interactive chat stream 300s timeout only checked between events: a stalled LLM/tool call hangs the SSE response and leaks the abandoned generator

### Truthful failure: stop rendering errors as success  `truthful-failure` [M] [BROKEN-NOW] (5 findings)

SSE errors become text with finishReason 'stop'; checkpointer/thread-list outages return []-with-200; cascade-FAILED rows report a fabricated cause + wrong recovery advice; login-resolution failures swallowed (user lands with no data, no error); panic-loop escalation never streamed live.

- [ ] `06#4` DEBT/correctness r2 -- Chat stream protocol swallows errors: timeouts/exceptions become text deltas with finishReason 'stop' — failed runs indistinguishable from success
- [ ] `07#7` LATENT/correctness r1 -- Outage renders as empty-but-successful UI: checkpointer/thread-list load failures return [] with HTTP 200
- [ ] `07#9` LATENT/correctness r1 -- Cascade-FAILED view schemas report a fabricated cause and wrong recovery advice
- [ ] `07#6` LATENT/velocity r2 -- Login-time tenant resolution failures swallowed: user lands with zero data sources and no error
- [ ] `06#1` BROKEN-NOW/correctness r1 -- Panic-loop escalation message never streamed to live UI; escalation detector couples to FastMCP JSON spacing

### Minimum observability: alarms, real health checks, destruction logging  `observability` [M] [BROKEN-NOW] (4 findings)

Zero CloudWatch alarms; static health check; no worker/MCP heartbeat; production audit log suppressed (INFO under root WARNING) with empty project_id; DROP SCHEMA CASCADE is silent on success (the 2026-06-10 forensic question is still unanswerable); share tokens/OAuth codes in access logs.

- [ ] `08#7` DEBT/velocity r3 -- No detection layer: zero CloudWatch alarms, static health check, no worker heartbeat; process-death journeys emit no operator signal
- [ ] `08#8` BROKEN-NOW/correctness r2 -- Django-side agent audit log dropped in production (INFO under root WARNING) and logs an always-empty project_id; MCP audit has no actor and is logger-only
- [ ] `08#9` DEBT/velocity r2 -- Schema destruction is silent on success: TTL janitor and teardown tasks emit zero log lines for DROP SCHEMA CASCADE
- [ ] `08#6` LATENT/security r1 -- Share tokens and OAuth codes transit uvicorn/nginx access logs into CloudWatch

### Close the second auth perimeter (stock allauth /accounts/)  `auth-perimeter-hardening` [M] (5 findings)

/accounts/ HTML auth is a live second registration perimeter: open self-registration, bypasses the custom rate limiter, login-CSRF/silent linking on GET (SOCIALACCOUNT_LOGIN_ON_GET + AUTO_CONNECT), no working email backend (kills password reset), OCS allowlist open by default.

- [ ] `13#9` LATENT/security r1 -- allauth /accounts/ HTML auth surface is a second, ungoverned registration/auth perimeter parallel to the SPA's /api/auth/ paths
- [ ] `14#0` LATENT/correctness r1 -- Production has no working email backend: allauth password reset and email verification cannot deliver (also dead-ends the merge gate)
- [ ] `14#1` LATENT/security r1 -- allauth HTML auth surface bypasses the custom rate limiter, splits the brute-force budget, and shares per-process LocMemCache
- [ ] `14#2` LATENT/security r1 -- SOCIALACCOUNT_LOGIN_ON_GET=True + EMAIL_AUTHENTICATION_AUTO_CONNECT=True: login-CSRF and silent forced account-linking on GET
- [ ] `07#2` DEBT/security r3 -- OCS email-domain allowlist is open by default and bypassed by no-email logins; .env.example contradicts the coded default

### Account-merge correctness (privilege propagation, metadata cascade)  `account-merge-correctness` [M] (4 findings)

merge_users OR-propagates is_staff/is_superuser from the deleted duplicate (invisible at the y/N prompt -- plausibly already prod state); the verified-email merge gate is subtle; conflict paths cascade-delete the duplicate's TenantMetadata (live + historical migration 0004).

- [ ] `11#4` DEBT/security r1 -- merge_users OR-propagates is_staff/is_superuser from the deleted duplicate onto the canonical account, invisible at the confirmation prompt
- [ ] `01#8` DEBT/correctness r6 -- Account merge gate requires a verified EmailAddress the system never produces; local signup bypasses allauth so OAuth-with-same-email can't link
- [ ] `04#1` LATENT/correctness r2 -- User-merge conflict path cascade-deletes the duplicate's TenantMetadata and can orphan connection wiring
- [ ] `11#9` DEBT/correctness r1 -- users migration 0004 dedup can cascade-delete TenantMetadata on the discarded duplicate membership (historical, one-time)

### Django admin lockdown + management-command fixes  `admin-lockdown` [M] (5 findings)

Admin is an unguarded privileged-write surface: editable state-machine rows re-arm DROP SCHEMA CASCADE, unthrottled login, plaintext tokens, self-escalation; registration inverted (dangerous rows editable, operator models absent); AgentLearningAdmin renders escaped HTML; setup_oauth_apps composes wrong env names; backfill_readonly_roles aborts on first drift.

- [ ] `11#3` LATENT/data-loss r1 -- Django admin is an unguarded privileged-write surface: editable state-machine rows can re-arm DROP SCHEMA CASCADE; unthrottled login bypasses allauth/auth rate limiter; plaintext OAuth tokens & client secrets exposed; self-escalation via UserAdmin
- [ ] `11#5` DEBT/velocity r1 -- Admin registration inverted: dangerous raw rows fully editable while every model operators need (ThreadJob, WorkspaceViewSchema, memberships, connections) is absent; RecipeAdmin manages vestigial RecipeStep and omits live Recipe.prompt
- [ ] `11#6` COSMETIC/velocity r1 -- AgentLearningAdmin confidence_badge relies on removed allow_tags; renders escaped literal HTML
- [ ] `11#7` LATENT/velocity r1 -- setup_oauth_apps composes wrong env-var names for Google/GitHub (double _OAUTH_); skip message and docstring name two further spellings, so Google/GitHub can never bootstrap via this command
- [ ] `11#8` LATENT/correctness r1 -- backfill_readonly_roles aborts on first Django-vs-physical schema drift, leaving later schemas role-less (fail-closed query outage); selects dead MATERIALIZING state; no per-schema error handling, dry-run, or view-role default privileges

### Infra/network security: credential separation, subnets, egress, CI role  `infra-network-security` [L] (4 findings)

One DB + one master-superuser credential for both planes; RDS/Redis in public subnets, SSH 0.0.0.0/0, admin internet-exposed; wide-open egress + IMDSv1 (loader SSRF -> instance-role theft); CI deploy role reads every RDS master password in the account. (Some items are prod-stack facts, not in-repo.)

- [ ] `11#0` LATENT/security r1 -- One DB, one master-superuser credential: platform and managed/tenant planes co-located, app connects as RDS master
- [ ] `11#2` DEBT/security r1 -- RDS/Redis in public subnets; broad management surface (SSH 0.0.0.0/0 + SSM); Django admin internet-exposed
- [ ] `10#8` LATENT/security r1 -- Wide-open egress + IMDSv1: loader SSRF unbounded, instance-role credential theft reachable
- [ ] `10#9` LATENT/security r1 -- CI deploy role over-scoped: account-wide secret prefix wildcards read every RDS master password

### Knowledge / Data Dictionary correctness fixes  `knowledge-fixes` [M] (4 findings)

TableKnowledge keyed by physical schema name (every refresh orphans annotations; multi-tenant can never match); autosave silently wipes related_tables; import 500s + round-trip loses duplicate-titled entries; learning lifecycle inert while the prompt implies usage. Candidate inputs to content-satellite-redesign.

- [ ] `01#5` LATENT/correctness r4 -- TableKnowledge keyed by physical schema-qualified name: refresh orphans annotations, multi-tenant mismatch, stale names injected into prompts
- [ ] `05#0` LATENT/data-loss r2 -- Data Dictionary annotation autosave silently wipes related_tables (and other list/dict fields) on every edit
- [ ] `05#8` LATENT/correctness r2 -- Knowledge import 500s on malformed input / duplicate titles; export-import round trip silently loses duplicate-titled entries
- [ ] `05#9` DEBT/correctness r2 -- Learning lifecycle is inert: confidence never auto-adjusts, times_applied effectively never increments, but the prompt implies real usage

### [DESIGN] Content-satellite redesign: recipes + knowledge + artifacts  `content-satellite-redesign` [L] [**DESIGN-GATED**]

The wave-1/2 fixes restore recipes/knowledge/artifacts to working-AS-DESIGNED. Whether that current design is what we want is a separate product question. Brainstorm -> spec before committing to repair-in-place vs rethink. References recipe-runner-fix, knowledge-fixes, artifact-multitenant-render, artifact-sandbox-isolation, permission-layer (recipe privacy).


References: `recipe-runner-fix`, `knowledge-fixes`, `artifact-multitenant-render`, `artifact-sandbox-isolation`, `permission-layer`

## Wave 3 -- tail, remaining guardrails, cleanup

### Provider loader data-quality + upstream-contract fixes  `provider-data-quality` [M] [BROKEN-NOW] (11 findings)

Unguarded inbound payloads (>255 names, missing keys, NUMERIC overflow); dead/wrong columns (raw_visits.images, participant_platform, unstable message_id, Connect count always None); next-URL trust (Connect plaintext http://); resumability split across two registries; offset-skip (Forms v0.5 only); denominator accuracy >10k.

- [ ] `02#7` LATENT/data-loss r4 -- Inbound provider payload sizes/shapes unguarded: long canonical_name DataErrors swallowed, missing/empty natural keys collapse or crash writers, NUMERIC out-of-range, money precision
- [ ] `12#8` DEBT/correctness r1 -- raw_visits.images is always empty: upstream only emits images with ?images=true, which Scout never sends, yet the writer creates and upserts an images JSONB column
- [ ] `12#9` DEBT/correctness r1 -- raw_sessions.participant_platform always '' (OCS nested participant serializer has no platform field); sessions->participants join on bare identifier conflates same-identifier participants across platforms
- [ ] `13#0` LATENT/correctness r1 -- OCS session detail interleaves synthetic unsaved 'summary' messages; Scout's positional message_id is unstable across syncs (raises the resumability-registry finding's blast radius)
- [ ] `12#7` COSMETIC/velocity r1 -- Connect v2 export responses never contain 'count': progress totals always None and loader docstrings describe a nonexistent field
- [ ] `13#2` DEBT/cost-perf r1 -- Case v2 matching_records accuracy above 10k unverified (possible ES total-hits cap on the progress denominator)
- [ ] `09#2` DEBT/correctness r1 -- CommCare offset pagination under live writes can silently skip records
- [ ] `12#5` LATENT/correctness r1 -- Known finding correction: Case API v2 cannot silently skip under live writes (keyset, dups-at-end by design); skip risk is Forms v0.5 only, narrower than claimed
- [ ] `09#1` LATENT/security r2 -- Loaders follow server-supplied next URLs anywhere with session-pinned credentials (no host/scheme validation)
- [ ] `12#6` BROKEN-NOW/security r1 -- Next-URL trust verified per provider: Case v2 next is settings-derived absolute (safe today); Forms v0.5 relative (handled); Connect next is proxy-header-derived and emitted plaintext http:// in prod (#1109), sending Bearer over plaintext first hop
- [ ] `03#7` DEBT/correctness r4 -- Resumability truth lives in two contradictory registries (_RESUMABLE_CONNECT_SOURCES vs YAML) with an unsafe default-True; non-resumable Connect writers still commit per page

### Reference-drift detection janitor (stored SQL/table refs)  `reference-drift-detection` [M] (1 finding)

No drift detection for any stored schema reference (artifact SQL, knowledge, learnings, recipes); every rename mechanism ships without migrating refs. Add a janitor that validates stored refs against live catalogs and flags rather than silently rotting.

- [ ] `06#0` LATENT/correctness r2 -- No drift detection for stored schema references anywhere; rename events (PR #228 prefix change, tenant-count transitions) silently invalidate artifacts/learnings/recipes/knowledge

### LangGraph checkpoint retention + deletion on member removal  `checkpoint-retention-privacy` [M] (1 finding)

Member removal deletes Thread rows but never LangGraph checkpoints; checkpoints are never pruned anywhere (retention/privacy gap + unbounded growth).

- [ ] `02#4` LATENT/data-loss r3 -- Member removal deletes Thread rows but never deletes LangGraph checkpoints; checkpoints never pruned anywhere

### Dead code / rename residue / cosmetic drift cleanup  `dead-code-cleanup` [M] (7 findings)

Dead DRF permission classes, dual checkpointer module (+ MemorySaver-in-prod footgun), project_id audit residue, domainSlice naming stratum, export 501, dead share surface, vestigial RecipeStep, execute_async; stale Celery docstrings, removed-model prompt sections, doubled heading, inline imports; TS type lies; minor run-lifecycle drift; the two REFUTED findings (kept for the record).

- [ ] `02#0` DEBT/velocity r7 -- Large dead-code / rename-residue cluster (permissions, dual checkpointer, project_id audit, domainSlice, export 501, dead share surface, legacy data_dictionary, RecipeStep, execute_async, etc.)
- [ ] `10#6` COSMETIC/velocity r7 -- Cosmetic drift cluster: stale Celery docstrings, removed-model prompt sections, doubled Knowledge Base heading, inline imports, three-strata naming residue
- [ ] `10#7` COSMETIC/velocity r3 -- Minor type lies and cosmetic seam drift: TS contracts stricter than runtime payloads, per-process prompt-cache key omits schema/knowledge state, list-repr in recipe results
- [ ] `03#8` COSMETIC/correctness r2 -- Minor run-lifecycle drift: terminal-run progress writes, unconditional result clobber, post-COMPLETED step-guard raise; materialized_row_count wrong after resumed run
- [ ] `07#8` DEBT/correctness r3 -- DEBUG MemorySaver fallback cached for process lifetime; dead memory checkpointer module carries a silent prod MemorySaver fallback
- [ ] `07#5` COSMETIC/correctness r1 -- auto_create_workspace_on_membership performs three writes outside a transaction
- [ ] `04#2` COSMETIC/correctness r2 -- Connect resumable writers can duplicate on stale-cursor replay; safety rests on an implicit invariant that the zombie-janitor fix would break

### [POLICY] Re-run the arch-review coverage matrix quarterly  `rerun-review-quarterly` [S]

The methodology is repeatable; diff the next findings DB against this run (2026-06-12) to measure remediation and catch drift.


