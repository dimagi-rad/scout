# Gap Round 2 — Runtime Verifier: Criticals

**Role:** Runtime verifier subagent. Mandate: empirically confirm/refute the 7 highest-severity findings that prior reviewers flagged as unreproduced static inference, with reproduction transcripts. Report only — no production code changed; read-only / disposable-fixture commands only.

**Environment notes:**
- Docker daemon (OrbStack) was down; `psql` binary not installed. Verification was done against the live local Postgres.app instance on `:5432` (the real `agent_platform` DB) using `uv run python` + `psycopg`, and via in-process `manage.py` / `django.setup()` harnesses.
- PostgreSQL server is **18.3**, not the PG16 used in `docker-compose.yml`. The semantics exercised here (`SET ROLE`, `pg_catalog` ACL behavior, 63-byte `NAMEDATALEN-1` identifier truncation) are identical across PG16–PG18, so the version delta does not affect any verdict.
- All disposable fixtures (schemas `av_secret_tenant`, `av_dbt_test`; exploit `User`/`Workspace`/`Artifact` rows; playwright session) were torn down after each test.

**Verdict tally:** 6 VERIFIED (a, b, c, d, f, g), 1 REFUTED (e).

---

## (a) RecipeRunner calls build_agent_graph with a removed `tenant_membership` signature — VERIFIED

- **Status:** BROKEN-NOW · **Impact:** correctness/velocity · **Confidence:** verified-by-trace

**Claim.** `apps/recipes/services/runner.py` `_build_graph` (lines 99–121) calls:
```python
await build_agent_graph(
    tenant_membership=self._tenant_membership,
    user=self.user,
    checkpointer=None,
)
```
but `apps/agents/graph/base.py:480` defines:
```python
async def build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)
```
There is no `tenant_membership` parameter (and `workspace` is required-positional). Any recipe run raises `TypeError` at graph construction.

**Why it is not caught.** `execute()` calls `async_to_sync(self._build_graph)()` at line ~191, *before* the `try/except` that wraps step execution (the try begins ~line 213). The `TypeError` therefore propagates uncaught and the `RecipeRun` row is left stuck in `RUNNING` (never transitioned to `FAILED`).

**Reachability.** `apps/recipes/api/views.py:105-108` — `RecipeRunner(...).execute()` is invoked from the authenticated endpoint `POST /api/recipes/<id>/run/`. Reachable in production by any workspace member who runs a recipe.

**Reproduction.** Instantiated `RecipeRunner` in-process under `django.setup()` and called the build path; Python raised:
```
TypeError: build_agent_graph() got an unexpected keyword argument 'tenant_membership'
```
The keyword/positional mismatch is a hard, deterministic failure — every recipe run is broken.

---

## (b) Generated CommCare dbt staging models fail on unqualified raw-table refs (no profile search_path) — VERIFIED

- **Status:** BROKEN-NOW · **Impact:** correctness · **Confidence:** verified-by-trace

**Claim.** `apps/transformations/services/commcare_staging.py` `_generate_case_type_asset` emits staging SQL that references the raw table **unqualified** (line ~144, `FROM raw_cases`). `mcp_server/services/dbt_runner.py` `generate_profiles_yml` (lines 28–61) writes **no** `search_path` key into the generated `profiles.yml`. With dbt's default Postgres `search_path` (effectively `"$user", public`), the unqualified `raw_cases` is unresolvable inside the tenant schema, so the staging model errors.

**Reproduction.** Ran real **dbt 1.11.6** against a disposable schema `av_dbt_test` containing a `raw_cases` table, using the actual `generate_profiles_yml` + `write_dbt_project` code and the exact generated staging SQL:
```sql
SELECT case_id, case_type, properties->>'age' AS "age"
FROM raw_cases
WHERE case_type = 'patient'
```
Results:
```
profiles.yml has search_path key? === False
Database Error in model stg_case_patient (...)
  relation "raw_cases" does not exist
  LINE 16: FROM raw_cases
dbt run success: False
```
Schema cleaned up afterward. Confirms the model cannot resolve its source under the generated profile.

---

## (c) Legacy `/refresh/` loads fresh data into the OLD schema, then destroys it — VERIFIED

- **Status:** BROKEN-NOW · **Impact:** data-loss · **Confidence:** verified-by-trace (code path; full materialization not executed)

**Claim & mechanism.** `apps/workspaces/tasks.py` `refresh_tenant_schema` (entry ~line 126):
1. Creates a new refresh schema (`create_refresh_schema`).
2. Calls `run_pipeline` (~line 173) to materialize fresh data.
3. Step 4 (~lines 186–197) tears down the old ACTIVE schema(s).

The defect is in step 2's data path: `mcp_server/services/materializer.py:183` does:
```python
tenant_schema = SchemaManager().provision(tenant_membership.tenant)
```
`provision()` (`apps/workspaces/services/schema_manager.py:57`) returns the **bare `t_<id>` OLD active schema**, *not* the freshly created refresh schema. So materialization writes the new data into the old schema, and then step 4 drops that very schema — destroying the just-loaded data and leaving the tenant with an empty/half-built refresh schema.

**Reachability.** `apps/workspaces/api/views.py:314` `RefreshSchemaView.post` → `refresh_tenant_schema.defer(...)`. Reachable via the authenticated refresh endpoint.

**Verification depth.** Verified by trace: `provision()` return value and the drop target were confirmed by reading the code paths and confirming `provision()` ignores any refresh-schema argument. I did **not** execute a full end-to-end materialization (MCP server / full honcho stack was not brought up because Docker was down), so this is verified-by-trace rather than by live end-to-end run.

---

## (d) Cross-tenant `pg_catalog` metadata reads succeed under `SET ROLE` readonly role — VERIFIED

- **Status:** BROKEN-NOW · **Impact:** security · **Confidence:** verified-by-trace

**Claim.** The MCP query path (`mcp_server/services/query.py:44`) does `SET ROLE {readonly_role}` then sets `search_path` as its tenant-isolation defense. `mcp_server/services/sql_validator.py` `_validate_table_access` (line ~269) only enforces the schema check `if table_schema:` (line ~277) — i.e. it only validates *schema-qualified* table references. Unqualified catalog reads (`pg_tables`, `pg_namespace`, `pg_class`, `pg_attribute`) carry no schema token and bypass the check. `pg_catalog` is world-readable, so under `SET ROLE` the tenant role can enumerate **other tenants'** schema/table/column metadata.

**Reproduction (live).** Created disposable schema `av_secret_tenant` with a table/columns, connected as the tenant readonly role, ran `SET ROLE`, and selected from `pg_catalog`:
- `pg_tables` / `pg_namespace`: returned rows for other tenants' schemas.
- `pg_attribute` (joined to `pg_class`): disclosed the secret table's **column names** across the tenant boundary.

Confirmed the validator passes these unqualified queries (no `table_schema` token → no enforcement). Cross-tenant *metadata* disclosure is real.

**Scope note (what's fine).** The corresponding live test of actual *data* `SELECT` across schemas was correctly **blocked** by `SET ROLE` (the readonly role lacks `USAGE`/`SELECT` on other tenants' schemas). So the leak is metadata/structure disclosure (table and column names), not row data — still a security finding, but bounded.

---

## (e) LangGraph checkpointer persists `config.configurable` (oauth_tokens) into checkpoint rows — REFUTED

- **Status:** corrected to COSMETIC / non-issue *for this specific claim* · **Impact:** (none for this claim) · **Confidence:** verified-by-trace

**Claim (as flagged).** The checkpointer decomposes `config.configurable` into checkpoint table columns, so OAuth tokens placed in config would be persisted at rest in checkpoint blobs.

**Why it is refuted.** `apps/chat/views.py:193-197` builds:
```python
config = {
    "configurable": {"thread_id": thread_id},
    "recursion_limit": 50,
    "oauth_tokens": oauth_tokens,
}
```
`oauth_tokens` is a **top-level sibling** of `configurable`, **not** inside `configurable`. `AsyncPostgresSaver` only persists `config["configurable"]` (specifically `thread_id` / `checkpoint_ns` / `checkpoint_id`). The top-level `oauth_tokens` is never written to a checkpoint row.

**Live confirmation.** Inspected actual checkpoint blobs in the live `agent_platform` DB: across **128 checkpoint rows**, `oauth_tokens` appears **zero** times in the persisted metadata/checkpoint payloads. The only `configurable` keys present are the standard thread identifiers.

**Note.** The separate, already-reported finding that the OAuth-token plumbing is largely dead weight (cost paid for tokens threaded through config but not used by the persisted graph) is *unaffected* by this refutation — that is a different claim. This refutation only addresses the "tokens persisted at rest in checkpoints" claim, which is false.

---

## (f) Artifact sandbox iframe (`allow-scripts` + `allow-same-origin`) can reach the user's session-authenticated API — VERIFIED

- **Status:** BROKEN-NOW · **Impact:** security · **Confidence:** verified-by-trace

**Claim.** `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx` (lines ~192/194) renders the artifact iframe with a **relative, same-origin `src`** and `sandbox="allow-scripts allow-same-origin allow-modals"`. The combination `allow-scripts` + `allow-same-origin` defeats the sandbox: artifact-authored JS runs in the parent origin and inherits the user's session cookies. `apps/artifacts/views.py` `generate_csp_with_nonce` (line ~34) sets `connect-src 'self'` and `script-src ... 'unsafe-eval'`, which permits same-origin `fetch` from inside the iframe and `new Function`/eval-based React rendering.

**Reproduction (live, playwright-cli).** Authenticated a real browser session, loaded an artifact whose script called `fetch('/api/auth/me/')`. The fetch **succeeded** and returned the logged-in user's identity payload — the sandboxed artifact reached the session-authenticated API. Chrome emitted the expected console warning that an iframe with both `allow-scripts` and `allow-same-origin` can escape its sandbox.

**Control.** Re-ran the identical artifact in an iframe **without** `allow-same-origin`: the same-origin `fetch` was blocked / returned opaque, and the call failed. This isolates `allow-same-origin` as the decisive attribute — confirming the escape is real and attributable.

---

## (g) Tenant schema/role name 63-byte truncation collision — VERIFIED

- **Status:** BROKEN-NOW · **Impact:** data-loss · **Confidence:** verified-by-trace

**Claim.** `apps/workspaces/services/schema_manager.py` `_sanitize_schema_name` (line ~625) lowercases and replaces hyphens but applies **no length cap**. Tenant schema names and the derived `readonly_role_name` (`{schema}_ro`) can exceed PostgreSQL's 63-byte `NAMEDATALEN-1` limit, where PG silently truncates. Two distinct tenants whose names share a long common prefix truncate to the **same** physical schema (and the same role), collapsing their isolation. (Note: the *view-name* path at lines ~335–350 *does* have 63-byte guards — but the tenant schema/role path does not.)

**Reproduction (live).** Created two tenants whose sanitized schema names differed only past byte 63. PostgreSQL truncated both to one physical schema:
- Two `CREATE SCHEMA` statements collapsed to a single physical schema.
- An `INSERT` performed as "tenant B" landed in a table created by "tenant A" — cross-tenant write into the shared physical schema.
- The derived `_ro` role names truncated to a single colliding role.

Two logically distinct tenants → one physical schema and one role → silent cross-tenant data commingling. Fixtures dropped afterward.

---

## What's fine (verified healthy)

- **Checkpointer does NOT leak OAuth tokens.** `oauth_tokens` is a top-level config sibling, never inside `configurable`; 0/128 live checkpoint rows contain it. (e)
- **`SET ROLE` correctly blocks actual cross-tenant *data* SELECT.** The readonly role lacks `USAGE`/`SELECT` on other tenants' schemas; only `pg_catalog` *metadata* leaks. (d)
- **View-name 63-byte guard works.** The sibling-view path (`schema_manager.py:335-350`) caps identifier length correctly; the bug is confined to the tenant schema/role path. (g)
- **Control iframe without `allow-same-origin` is properly isolated.** Removing that one attribute blocks the same-origin API fetch — sandboxing is effective when configured correctly. (f)

---

## Coverage log

**Deep (read line-by-line / executed):**
- `apps/recipes/services/runner.py`, `apps/agents/graph/base.py` (signature), `apps/recipes/api/views.py`
- `apps/transformations/services/commcare_staging.py`, `mcp_server/services/dbt_runner.py`, `apps/transformations/services/dbt_project.py`, `apps/transformations/services/executor.py`
- `apps/workspaces/tasks.py` (refresh path), `mcp_server/services/materializer.py`, `apps/workspaces/services/schema_manager.py`, `apps/workspaces/api/views.py` (RefreshSchemaView)
- `mcp_server/services/sql_validator.py`, `mcp_server/services/query.py`
- `apps/chat/views.py` (config build), `apps/agents/memory/checkpointer.py`, `apps/chat/checkpointer.py`
- `apps/artifacts/views.py` (CSP/sandbox), `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx`

**Skimmed:**
- `docker-compose.yml`, `docs/arch-review/2026-06-12/cartography.md`, `docs/arch-review-methodology.md`

**NOT examined / verification limits (honest):**
- Did **NOT** execute a full end-to-end materialization for (c); verified by code trace only (MCP server / honcho stack not brought up because Docker/OrbStack was down).
- Did **NOT** bring up the MCP server or full honcho stack; did NOT exercise the actual MCP query tool over the wire (the (d) validator-bypass + catalog reads were verified against live PG and by reading the validator, not by driving the MCP tool end-to-end).
- Tested on **PostgreSQL 18.3**, not the PG16 in docker-compose. `SET ROLE` / `pg_catalog` ACL / 63-byte truncation semantics are identical across these versions, so verdicts hold.
- Verified only the 7 assigned findings; did not hunt for new issues (per mandate).
