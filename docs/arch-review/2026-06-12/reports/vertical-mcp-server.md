# Vertical review: MCP server (tools, envelope, auth model, context routing, direct managed-DB access)

*Reviewer: vertical:mcp-server. Repo HEAD 35e4230, 2026-06-12. Report only — no code changed.*

## Scope as reviewed

`mcp_server/` excluding loader internals and the materializer's writer functions (owned by
the loaders/materializer vertical): `server.py`, `context.py`, `envelope.py`, `auth.py`,
`services/query.py`, `services/sql_validator.py`, `services/metadata.py`,
`pipeline_registry.py`, `__main__.py`. Plus the consumers of MCP context/contract on the
Django side: `apps/agents/graph/base.py` (tool schema rewriting + injection),
`apps/agents/mcp_client.py`, the caller-side checks in `apps/chat/views.py`, the
state contract in `apps/recipes/services/runner.py`, the direct-context reuse in
`apps/artifacts/views.py`, and the worker-side halves of the MCP contracts in
`apps/workspaces/tasks.py` and `services/schema_manager.py`.

---

## 1. The trust model: what the MCP server assumes callers have already checked

The MCP server has **no authentication of its own**. The streamable-HTTP endpoint
(`server.py:904-914`) configures only `TransportSecuritySettings` (DNS-rebinding/Host-header
checking, `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "scout-mcp-web:*"]`) —
that is not authn. There is no token, no shared secret, no per-request principal. The whole
security model is:

| Check | Who is assumed to have done it | Where |
|---|---|---|
| User is authenticated | Django chat view | `chat/views.py:75` (`request._authenticated_user`) |
| User is a member of the workspace | Django chat view | `chat/views.py:109-114` (`_resolve_workspace_and_membership`) |
| Thread belongs to user+workspace | Django chat view | `chat/views.py:116-137` |
| `workspace_id`/`user_id`/`thread_id` are the *right* ones | agent graph injection node | `graph/base.py:452-475` (overrides LLM-supplied args for tools in `MCP_TOOL_NAMES`) |
| LLM cannot supply context IDs | `_llm_tool_schemas` hides them | `graph/base.py:396-436` |
| Rate limiting | chat layer | `apps/chat/rate_limiting.py` — MCP itself is unlimited |

Inside that perimeter, enforcement is **inconsistent across tools**:

- `run_materialization` re-checks tenant membership and thread ownership itself
  ("Defense in depth … since this tool is the one persisting the trust boundary",
  `server.py:553-570`).
- `query`, `list_tables`, `describe_table`, `get_metadata`, `get_schema_status`, and the
  **destructive `teardown_schema`** take only `workspace_id` and bind to no user at all.
- `cancel_materialization` and `get_materialization_status` take a bare `run_id` with **no
  workspace or user scoping at all**, and because they are *not* in `MCP_TOOL_NAMES`
  (`graph/base.py:65-76`), the LLM supplies `run_id` freely — nothing is injected or hidden.

Network reachability: prod runs MCP as a Kamal service on the `scout_shared` docker network
with `--host 0.0.0.0` and no proxy (`config/deploy-mcp.yml`); docker-compose uses `expose`
(internal) not `ports`; dev binds 127.0.0.1 (`Procfile.dev`). So the endpoint is not
internet-reachable, but **any process on the shared network (frontend nginx container, api,
worker) can invoke any tool for any workspace**, including `teardown_schema`. The TODO.md
security section items (per-tenant role isolation wording, append-only MCP audit table,
loader egress restriction) are all unchecked; the audit trail is a stderr logger
(`envelope.py:22`, `tool_context`) with no `LOGGING` config in settings — it survives only
as CloudWatch container logs.

---

## 2. Findings

### F1. MCP `teardown_schema` drops physical schemas but never updates platform-DB state — bypasses the entire teardown protocol

**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

Chain:
- Entry: MCP tool `teardown_schema` (`server.py:802`), exposed to the LLM (in
  `MCP_TOOL_NAMES`, `graph/base.py:73`), docstring invites the agent to call it "when a
  failed materialization has left the schema in an unrecoverable state".
- `server.py:848` `await mgr.ateardown_view_schema(vs)` and `server.py:857`
  `await mgr.ateardown(ts)` — these only `DROP SCHEMA … CASCADE` + drop the role
  (`schema_manager.py:474-512`); the docstrings say "callers are responsible for updating
  the model state".
- The tool **never** sets `ts.state`/`vs.state`, never marks `MaterializationRun`s STALE,
  and never calls `_fail_dependent_view_schemas`.

Compare the worker task that does this correctly: `tasks.py:610-661` (`teardown_schema`
task) drops, then `_fail_dependent_view_schemas(schema.tenant_id)` (tasks.py:653), marks
runs STALE, marks the schema EXPIRED, and restores ACTIVE on failure.

Consequences after the agent tears a workspace down via MCP:
1. `TenantSchema`/`WorkspaceViewSchema` rows remain ACTIVE pointing at dropped physical
   schemas. `get_schema_status` (single-tenant path, `server.py:689-735`) then reports
   `exists: True, state: active` **with a table list read from the stale
   `MaterializationRun.result`** — phantom tables, while `query` fails ("role … does not
   exist" / relation errors). This is exactly the contradictory-tool-responses class that
   produced the #190 panic loops.
2. Tenant schemas are shared across workspaces (acknowledged at `tasks.py:339-344`).
   `DROP SCHEMA t_x CASCADE` cascade-drops the namespaced views inside **sibling
   workspaces'** view schemas; the PR #230 sibling-rebuild machinery
   (`_rebuild_sibling_view_schemas`, tasks.py:441) lives only in the worker task path and
   is never triggered by this tool, so sibling workspaces stay ACTIVE-but-empty until the
   torn-down workspace happens to re-materialize.
3. Partial self-heal exists: the next `materialize_workspace` → `provision()` →
   `_ensure_physical_schema` (`schema_manager.py:76,131-148`) recreates schema+role, and a
   completed run supersedes the stale catalog. The broken window is everything between
   teardown and the next successful materialization.

Reachable via: live MCP tool in every chat session. Also note the abuse surface: a
destructive, user-unbound tool guarded only by `confirm=True`, callable by an LLM that
reads untrusted query results (prompt-injection → data teardown for tenants shared with
other workspaces). Data is re-materializable, so impact is correctness/disruption rather
than permanent data-loss.

### F2. `cancel_materialization` writes FAILED, but the worker only stops on CANCELLED — the two cancel implementations disagree

**Status: BROKEN-NOW · Impact: correctness + cost-perf · Confidence: verified-by-trace · Complexity: accidental**

Chain:
- MCP tool sets `run.state = MaterializationRun.RunState.FAILED` (`server.py:479`) and
  touches nothing else (no procrastinate abort, no ThreadJob update).
- The worker's cancellation checkpoint raises only on CANCELLED:
  `tasks.py:493` `if current_state == MaterializationRun.RunState.CANCELLED: raise MaterializationCancelled()`.
- The HTTP cancel endpoint does it right: `materialization_views.py:104`
  `aupdate(state=RunState.CANCELLED, …)` plus job abort ("The DB state must be flipped
  *before* signalling procrastinate", lines 26-36).

Consequence of an MCP-tool cancel during LOAD: the per-page checkpoint never fires, the
loader runs the source to completion (full provider-API + DB cost), the terminal CAS
(`filter(state__in=ACTIVE_STATES)`) preserves the external FAILED, and the freshly loaded
data is committed but **invisible to the catalog** (`pipeline_list_tables` only reads
COMPLETED/PARTIAL runs, `metadata.py:57-67`). The DISCOVER→LOADING CAS
(`materializer.py:238-243`) happens to catch a FAILED write made during DISCOVER, so the
tool "works" only in that narrow phase. The tool's docstring even states "Marks the run as
failed" — drift codified. Reachable via: tool is loaded and bound to the LLM (passes
through `_llm_tool_schemas` unmodified).

### F3. Recipe runner's agent state predates the workspace rename — every MCP tool call from a recipe gets `workspace_id=""`

**Status: BROKEN-NOW (for recipes×MCP) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental (rename residue)**

Chain:
- `recipes/services/runner.py:215-224` and `:302-311` build `initial_state` with
  `tenant_id`, `tenant_name`, `tenant_membership_id`, `user_id`, `user_role` — no
  `workspace_id`, no `thread_id`. `AgentState` (`graph/state.py:80-110`) has
  `workspace_id`/`user_id`/`user_role`/`thread_id`; the tenant-era keys no longer exist.
- The injection node fills MCP args from state with empty-string fallback:
  `graph/base.py:461` `extra = {k: state.get(v, "") for k, v in injections.items()}`.
- Every MCP tool then errors: `server.py:73-74` `if not workspace_id: raise ValueError("workspace_id is required")`
  → `VALIDATION_ERROR` envelope.
- Three consecutive such errors trip the escalation breaker
  (`graph/base.py:87-123`), ending the run with the canned "repeated schema errors"
  message.

So a recipe whose prompt requires any data access (which is the point of recipes) cannot
query through MCP at all. Reachable via: `/api/workspaces/<id>/recipes/<id>/run` and the
recipes UI. This is the recipes↔graph signature drift v1 flagged, seen here from the MCP
contract side.

### F4. `get_metadata` silently returns an empty snapshot for multi-tenant workspaces

**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

Chain: multi-tenant workspace → `_resolve_mcp_context` returns the `ws_*` view-schema
context (`context.py:113-139`) → `server.py:248`
`ts = await TenantSchema.objects.filter(schema_name=ctx.schema_name).afirst()` finds
nothing for a `ws_*` name → early-return `{"table_count": 0, "tables": {}, …}` as a
**success** envelope (`server.py:249-255`). Meanwhile `list_tables` has an explicit
view-schema branch (`server.py:126-137`) and works. The agent is told, with
`success: true`, that a workspace whose `list_tables` shows N views has zero tables —
contradiction fodder for the escalation breaker. `describe_table` survives only because it
goes through `information_schema` (`metadata.py:199-207`); it silently uses the
`commcare_sync` pipeline fallback for descriptions (`server.py:78-101`) and no JSONB
annotations.

### F5. The system prompt and the `list_tables` tool compute the table list differently — transformation assets appear in one and not the other

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The prompt-side context uses `transformation_aware_list_tables` when terminal
`TransformationAsset`s exist (`graph/base.py:246-251`), which *replaces* raw tables with
terminal models. The MCP `list_tables` tool always uses plain `pipeline_list_tables`
(`server.py:161`) — `transformation_aware_list_tables` has **no non-test caller in
`mcp_server/`**. A workspace with custom transformations gets a system prompt advertising
terminal models while the tool the prompt tells the agent to trust lists the replaced raw
tables. Both lists point at physically existing tables, so queries still work; the damage
is contradictory schema responses — the documented precursor of panic loops (#190), in a
codebase that already shipped a circuit breaker for exactly this. Same family:
`_fetch_schema_context` selects the pipeline by `tenant.provider`
(`graph/base.py:217`) while the tool prefers `last_run.pipeline` (`server.py:91-95`).

### F6. Cross-tenant metadata disclosure through unqualified `pg_catalog` reads — known and even advertised to the LLM

**Status: BROKEN-NOW (by design choice, undocumented as a risk) · Impact: security · Confidence: strong-inference · Complexity: accidental**

- The validator checks schema qualification only: `_validate_table_access`
  (`sql_validator.py:269-288`) rejects `t_other.x` but says nothing about **unqualified**
  names, which PostgreSQL resolves through the implicit `pg_catalog` entry of every
  search_path.
- `SET ROLE {schema}_ro` (`query.py:44`) does not restrict catalog visibility:
  `pg_namespace`, `pg_class`, `pg_views`, `pg_tables` are world-readable in PG 16.
- Tenant schema names are derived directly from `tenant.external_id`
  (`schema_manager.py:625-631`) — i.e. CommCare domain names / customer identifiers — and
  `pg_class.reltuples` exposes approximate row counts of every other tenant's tables.
- The system prompt explicitly invites this: `prompts/base_system.py:167` — "Unqualified
  `pg_catalog` views (`pg_namespace`, `pg_class`, `pg_views`, `pg_tables`) are reachable
  if you really need raw system-state introspection".

So any chat user (or injected instructions inside query results) can enumerate the full
customer list and per-customer table inventory of the shared managed DB via the `query`
tool. I did not execute this against a live DB (hence strong-inference, not
verified-by-trace), but every link is quoted code or default PG 16 behavior.

### F7. SQL validator gaps where `SET ROLE` is the only real defense; one read-path gap has no backstop

**Status: LATENT · Impact: security · Confidence: strong-inference · Complexity: mixed**

`_validate_statement_type` (`sql_validator.py:221-246`) checks only the **top-level**
expression type. Constructs that parse as `exp.Select` pass:
- `SELECT … INTO new_table FROM …` (a write) — blocked in practice only because the `_ro`
  role lacks CREATE (PG 16 also removed PUBLIC CREATE on `public`).
- Data-modifying CTEs (`WITH x AS (INSERT …) SELECT …`) — blocked only by role grants.
- `pg_sleep`, `set_config` are absent from `DANGEROUS_FUNCTIONS` (30 s statement_timeout
  caps the damage).
- sqlglot↔PostgreSQL parse divergence remains the standing risk class for the
  single-statement rule (hypothesis only; no concrete bypass found).

The layering (validator + `SET ROLE` + statement_timeout + autocommit) is genuinely good
defense-in-depth for *writes*; F6 is the case where the role layer cannot help. Worth
adding `SELECT INTO`/CTE-DML checks and the two functions to keep the validator honest
with its own docstring ("Only SELECT statements allowed").

### F8. The OAuth-token transport into MCP is dead plumbing with a false docstring; chat pays for it on every turn

**Status: DEBT (dead code) · Impact: velocity (+minor cost) · Confidence: verified-by-trace · Complexity: accidental (vestige)**

- `mcp_server/auth.py:13` `extract_oauth_tokens` — **zero callers** in non-test code; its
  docstring claims "Tokens are injected by the Django chat view at the transport layer" —
  false today (comments are claims; this one fails verification).
- `build_agent_graph(…, oauth_tokens=…)` accepts the param and never reads it
  (`graph/base.py:485` — only the docstring mentions it).
- `chat/views.py:162,196` and `tasks.py:1143-1154` fetch SocialTokens every turn and put
  them in LangGraph `config["oauth_tokens"]` — no consumer anywhere in `apps/agents`.
- Related vestiges: `TenantContext` dataclass (`context.py:34-44`, zero usage),
  `AUTH_TOKEN_EXPIRED` (`envelope.py:31`, no non-test use), `_SCRUB_KEYS`
  (`envelope.py:82`) scrubbing a field that can no longer appear,
  `execute_internal_query` (`query.py:96`, zero non-test callers), `success_response`'s
  `project_id` kwarg (projects-era residue, never passed).
- Real credentials flow via `aresolve_credential(tm)` in the worker (`tasks.py:265`).
  Tests at `tests/test_mcp_server.py:211-242` keep the dead path green.

### F9. `run_materialization` thread-scoped dedupe knowingly permits concurrent materializations of the same tenant schema

**Status: LATENT · Impact: correctness (data integrity) · Confidence: verified-by-trace (the gap is documented in-code) · Complexity: essential problem, accidental gap**

`server.py:577-590`: the in-flight guard is scoped to `thread_id`, and the comment states
plainly: "this lets two threads in the same workspace dispatch parallel materializations
that share tenant_schemas … the materializer has no advisory lock per tenant_schema."
Two chat threads (or two workspaces sharing a tenant) can run concurrent
DROP/CREATE/INSERT against the same `t_*` tables. The ThreadJob create-order race is the
adjacent known issue: MCP creates the ThreadJob *after* `defer_async`
(`server.py:606-635`), the worker hedges ~3.75 s and falls back to the janitor
(`tasks.py:364-377`, TODO at :373 admits the cleaner fix). Both are acknowledged debt at
the exact seam with the worst incident history.

### F10. Envelope guarantee is partial: several tool paths raise instead of returning the envelope

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The envelope contract ("Every tool response is wrapped", `envelope.py:2-9`) holds for
validation failures and for `query` (which classifies DB errors, `query.py:148-172`), but:
- `get_schema_status` multi-tenant: `server.py:786` calls `_resolve_mcp_context` **outside**
  any try; the row that passed the `[ACTIVE, MATERIALIZING]` filter at :738-741 is
  re-fetched in `load_workspace_context` with `state=ACTIVE` only (`context.py:115-118`) —
  a MATERIALIZING view schema would raise an uncaught ValueError. Today nothing sets
  `WorkspaceViewSchema` to MATERIALIZING (no setter found in non-test code), so the branch
  is dead-but-armed; the state-filter mismatch is the finding.
- `list_tables` view-schema branch (`server.py:131`), `describe_table`
  (`server.py:216` → `metadata.py:199`), `get_schema_status` (`server.py:787`): managed-DB
  errors in `_execute_async_parameterized` propagate uncaught. FastMCP converts them to a
  generic `isError` result whose text does not contain `"code": "…"`, so these errors are
  also invisible to the escalation breaker's substring match (`graph/base.py:87`), which
  itself is coupled to `json.dumps` spacing — a fragile cross-process contract.

### F11. Tenant-schema names have no length guard — the 63-byte identifier bug class fixed for view names (PR #227) has an unfixed sibling at schema level

**Status: LATENT · Impact: correctness · Confidence: strong-inference · Complexity: accidental**

`_view_prefix` was carefully capped at 32 chars with a hash suffix after the 2026-06-10
incident (`schema_manager.py:219-241`), and `build_view_schema` hard-fails oversized view
names (:335-350). But `_sanitize_schema_name` (`schema_manager.py:625-631`) — the function
that produces `CREATE SCHEMA` identifiers from `tenant.external_id` — has **no length
check**. An external_id ≥ 63 bytes after sanitization would be silently truncated by
PostgreSQL; two tenants differing only beyond byte 63 collide into one schema, and the
readonly role name (`{schema}_ro`) truncates differently from the schema name.
Exploitability depends on provider ID length limits (CommCare domains are typically short;
OCS/Connect identifiers unverified by me) — hence LATENT. The incident seed explicitly
calls identifier-length bugs a recurring family; this is the remaining open site in the
schema-naming path.

### F12. Artifacts live-query path routes multi-tenant workspaces to an arbitrary single tenant's schema

**Status: BROKEN-NOW (multi-tenant artifacts) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

(Reported here because it's a consumer of MCP context routing; the artifacts vertical
owns the surface.) `artifacts/views.py:795-800`:
`tenant = await artifact.workspace.tenants.afirst()` then
`load_tenant_context(tenant.external_id)` — never `load_workspace_context`. Artifact SQL
authored in a multi-tenant chat references `prefix__table` views that exist only in the
`ws_*` schema, so every live artifact query in a multi-tenant workspace fails (and which
tenant `afirst()` picks is ordering-dependent). The MCP layer got multi-tenant routing
(`context.py:83-139`); this sibling consumer was never migrated.

### F13. Stale TODO.md vs implemented role isolation

**Status: COSMETIC · Impact: velocity · Confidence: verified-by-trace**

TODO.md:39 lists "PostgreSQL role isolation … use `SET ROLE` at query time" as unchecked,
but per-schema `_ro` roles + `SET ROLE` shipped in #91 and are live (`query.py:44`,
`schema_manager.py:588-623`). The unchecked list misstates the actual posture (the real
remaining gaps are F6/F7, the audit table, and loader egress).

---

## 3. Capability scorecard — how functional is each tool really?

| Tool | Demo path | Integration edges | Functional % | Notes |
|---|---|---|---|---|
| `query` | works | works for view schemas (role grants in `build_view_schema:379-405`); pg_catalog leak (F6); validator gaps (F7); new connection per call (no pool) | ~90% | the strongest tool |
| `list_tables` | works | multi-tenant branch works (names only, no descriptions/counts); omits TransformationAssets (F5); reconciliation vs information_schema is solid (#185/#187 handling in `metadata.py:29-112`) | ~85% | |
| `describe_table` | works | multi-tenant: wrong pipeline fallback, no annotations; DB errors break envelope (F10) | ~80% | |
| `get_metadata` | works single-tenant | **returns empty for multi-tenant (F4)** | ~50% | |
| `get_lineage` | works | properly workspace/tenant-scoped | ~95% | |
| `list_pipelines` | works | static registry; YAML parse failures swallowed with log only (`pipeline_registry.py:88-95`) | ~95% | |
| `get_schema_status` | works | phantom tables after MCP teardown (F1); armed crash on MATERIALIZING view schema (F10); FAILED view-schema surfacing (post-#229) is good | ~75% | |
| `run_materialization` | works | the only tool with its own authz; known ThreadJob ordering race + per-tenant concurrency gap (F9) | ~85% | most-hardened tool |
| `get_materialization_status` | works | unscoped `run_id` (cross-workspace read of run state/result given a UUID) | ~85% | |
| `cancel_materialization` | appears to work | **wrong state written; doesn't stop LOAD; loaded data becomes invisible (F2)**; unscoped run_id | ~35% | |
| `teardown_schema` | drops data as asked | **leaves all platform-DB state stale; breaks sibling workspaces; no user binding (F1)** | ~45% | |

Envelope: ~90% consistent (F10 paths excepted). Context routing (`context.py`): correct for
both topologies after the June fixes; the highest-fix-density file in the repo is now small
and clean — its residual risks live in its *callers* (F4, F12) rather than in the file.

## 4. Essential vs accidental complexity

Essential: two schema topologies (tenant vs view schema), three credential providers, and
fire-and-ack materialization genuinely require routing logic and a job-tracking handshake.

Accidental: (a) the same routing/status logic implemented twice — once in `server.py`
tools, once in `graph/base.py` prompt assembly — with diverging table-list and
pipeline-selection rules (F5); (b) two cancel implementations with different state
protocols (F2); (c) two teardown implementations with different state protocols (F1);
(d) the dead OAuth transport layer (F8); (e) Django apps importing `mcp_server` internals
(`graph/base.py:47-54`, `artifacts/views.py:25`) — the "MCP server" is simultaneously a
process boundary and a shared library, which is exactly why its callers drift.

## 5. What's fine

- `context.py` itself: clean two-path routing, defensive schema-name re-validation before
  embedding in connection options (`_parse_db_url:147-148`), TTL touch on access.
- `query` execution layer: SET ROLE + search_path + statement_timeout + autocommit, with
  CAS-style error classification; role lifecycle (create/grant/default-privileges, revoke
  before drop) in `schema_manager.py` is careful and idempotent.
- SQL validator core: single-statement, SELECT-only top level, 40+ function denylist,
  LIMIT injection incl. non-literal LIMIT capping, CTE-alias-aware table extraction.
- Catalog truth reconciliation (`pipeline_list_tables` + `_live_tables_in_schema`): the
  #185/#187 phantom-row fixes are real and well-commented.
- `run_materialization`'s guard rails: membership re-check, thread ownership re-check,
  dispatch rollback on ThreadJob create failure, in-flight dedupe with an honest comment
  about its scope.
- Post-incident view-schema work (#227-#230): bounded prefixes, pre-DDL collision and
  length checks, FAILED-state surfacing through `get_schema_status`, sibling rebuilds (in
  the worker path).
- MCP client circuit breaker (`mcp_client.py`) — simple and adequate.
- Test coverage breadth for this component is genuinely good (envelope, validator,
  role isolation, workspace routing, fire-and-ack) — though it also pins dead code (F8).

## 6. Coverage log

Deep-read (line-by-line): `mcp_server/server.py`, `mcp_server/context.py`,
`mcp_server/envelope.py`, `mcp_server/auth.py`, `mcp_server/services/query.py`,
`mcp_server/services/sql_validator.py`, `mcp_server/services/metadata.py`,
`mcp_server/pipeline_registry.py`, `mcp_server/__main__.py`,
`apps/agents/graph/base.py`, `apps/agents/mcp_client.py`,
`apps/workspaces/services/schema_manager.py`, `config/deploy.yml`, `config/deploy-mcp.yml`.

Skimmed (targeted sections only): `apps/workspaces/tasks.py` (materialize_workspace entry,
progress updater, teardown tasks, resume hedge), `mcp_server/services/materializer.py`
(cancellation/CAS regions only — lines ~85-330, 595-640), `apps/chat/views.py` (authz +
state assembly), `apps/recipes/services/runner.py` (state dicts), `apps/agents/graph/state.py`,
`apps/artifacts/views.py` (lines 770-830), `apps/agents/prompts/base_system.py` (tool
references), `apps/workspaces/api/materialization_views.py` (grep-level),
`apps/workspaces/services/workspace_service.py` (touch function), `docker-compose.yml`,
`Procfile.dev`, `TODO.md` (security section), test directory listing + teardown-test grep.

NOT examined (honest gaps for the gap loop):
- All 19 `mcp_server/loaders/*` files and the materializer's per-table writers (deferred to
  the loaders vertical per the cartography roster note).
- `mcp_server/services/dbt_runner.py` and the transform phase.
- The MCP progress-notification path (FastMCP `Context`/`ctx` usage) and
  `langchain_mcp_adapters` serialization behavior — my F10 claim about error-text shape is
  inferred, not observed on the wire.
- `tests/test_mcp_*` bodies (what the mocks hide) beyond grep level.
- Live verification of F6/F7 against a running PG 16 (no queries executed).
- `apps/workspaces/api/views.py` refresh path, jobs views, the janitors' internals.
- Frontend consumers of MCP-derived status shapes.
- Whether any loader/base URL is user-controllable (SSRF → unauthenticated MCP reachability
  from inside the network) — flagged but not traced.
