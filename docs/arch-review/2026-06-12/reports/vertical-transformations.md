# Vertical Review: Transformations / dbt Subsystem

*Reviewer: `vertical:transformations`. Scope: `apps/transformations/` (1,236 LOC), `mcp_server/services/dbt_runner.py`, the transform integration points in `mcp_server/services/materializer.py` and `apps/workspaces/tasks.py`, the listing/lineage seams in `apps/agents/graph/base.py` and `mcp_server/services/metadata.py`, and the `/api/transformations/` DRF surface. Report-only; no code changed.*

HEAD `35e4230`, branch `main`. Evidence standards per `docs/arch-review-methodology.md`.

---

## TL;DR — what percentage actually works

The transformations subsystem is **two parallel, partially-wired transform mechanisms** that the cartography map collapses into one:

1. **Dynamic per-tenant staging assets** (`TransformationAsset` rows generated from CommCare metadata, executed by the three-stage dbt executor). This is the path that runs during materialization.
2. **Static pipeline `dbt_models`** declared in `pipelines/*.yml` — *empty for every shipped pipeline* (commcare/connect/ocs declare no `dbt_models`). This path is dead weight in `pipeline_list_tables`.

Honest functional assessment of the dynamic path:

| Capability | % functional | Notes |
|---|---|---|
| Generate staging `TransformationAsset` records from CommCare metadata | ~90% (record-level) | `upsert_system_assets` works; never **deletes** stale assets (F5) |
| **Execute** those assets via dbt against the tenant schema | **~0% in production (strong inference)** | Generated SQL uses unqualified `FROM raw_cases`; dbt profile sets no `search_path` → models cannot resolve raw tables (F1) |
| Surface terminal models to the agent | partial / drifted | Only the system-prompt context is transformation-aware; the `list_tables`/`describe_table` MCP tools the agent calls are not (F4) |
| Lineage (`get_lineage` tool, `/lineage/` endpoint) | works as metadata lookup | Reads `TransformationAsset` rows; correct cross-tenant scoping (see What's Fine) |
| Workspace-scope transforms | **never run in the normal flow** (F3) | `_run_transform_phase` omits `workspace=`; only the UI-less `/runs/trigger/` endpoint runs them |
| Any user-facing UI | **0%** | Zero frontend references to transformations/lineage/dbt/assets |
| Real (non-mocked) test coverage of dbt execution | **0%** | Every executor/runner test mocks `run_dbt`/`dbtRunner` (F8) |

The subsystem was built March–April 2026 (milestones M3–M7, 19 commits, last functional change 2026-04-22) and has not been integrated with the May/June materialization + view-schema work. It predates the multi-tenant view-schema lifecycle and shows it.

---

## Findings

### F1 — dbt models can't resolve raw tables: profile omits `search_path`, generated SQL is unqualified (BROKEN-NOW / correctness, strong-inference)

**Mechanism.** The CommCare staging generator emits model SQL that references raw tables **unqualified**:

- `apps/transformations/services/commcare_staging.py:144` → `lines.append("FROM raw_cases")`
- `apps/transformations/services/commcare_staging.py:198` → `lines.append("FROM raw_forms")`

dbt creates each model *relation* in the configured schema (`schema_name`), but the body of the `CREATE TABLE … AS ( <model sql> )` is passed verbatim to Postgres (`.venv/.../dbt/include/global_project/macros/relations/table/create.sql`, `postgres__create_table_as`). So `FROM raw_cases` is resolved by the **session `search_path`**, not by the model's target schema.

The dbt profile generator never sets `search_path`:

- `mcp_server/services/dbt_runner.py:28-61` (`generate_profiles_yml`) writes only `host/port/user/password/dbname/schema/threads`. It parses the URL with `urlparse` and **drops any query string**, so even a `?options=-csearch_path=…` on `MANAGED_DATABASE_URL` would be discarded.
- dbt-postgres only applies a search_path when the profile carries one: `.venv/.../dbt/adapters/postgres/connections.py:114-119` (`if search_path is not None and search_path != "": kwargs["options"] = "-c search_path=…"`). With no key, the connection uses Postgres' default `"$user", public`.
- No `ALTER ROLE … SET search_path` is issued anywhere (`grep search_path` over `apps/` + `mcp_server/` shows it set only on the **query** paths — `mcp_server/context.py:159`, `mcp_server/services/query.py:47,77` — never for dbt). The codebase clearly *knows* unqualified access needs a search_path; the dbt profile is the one place it was omitted.

The connecting role is the managed-DB superuser (`MANAGED_DATABASE_URL`, the same role that runs `CREATE SCHEMA`/`CREATE ROLE` in `schema_manager.py`); its `$user` schema is not the tenant schema (`t_<sanitized external_id>`). Therefore `FROM raw_cases` resolves against `public`/`$user`, where `raw_cases` does not exist → **every generated staging model fails with "relation … does not exist".**

**Reachability.** The transform phase runs during materialization for any `commcare` tenant: system staging assets are generated at `mcp_server/services/materializer.py:217-234` (commcare-only), and the transform phase fires whenever any `TransformationAsset` exists for the tenant (`materializer.py:448` `has_assets = TransformationAsset.objects.filter(tenant=…).exists()` → `:452` `_run_transform_phase`). So this is on the live commcare materialization path.

**Why it's invisible.** (a) Transform failures are isolated by design — `materializer.py:455-457` catches everything and the run still transitions to COMPLETED (`:471-477`). (b) The error is stored under `result["transforms"]["error"]` but the resume aggregator only reads `result["sources"]` (`apps/workspaces/tasks.py:961-982`), so the agent/user is never told. (c) The executor itself logs at WARNING and swallows (`executor.py:184-185`). (d) Every test mocks dbt (F8), so CI is green.

**Confidence.** strong-inference, not verified-by-trace: I did not execute dbt against a live Postgres (out of scope for a read-only review). The chain — profile has no search_path, generated SQL is unqualified, dbt-postgres only sets search_path from the profile, no ALTER ROLE fallback — is tight and uses standard Postgres name resolution. The one unexecuted link is dbt's runtime SQL emission, which `postgres__create_table_as` makes explicit. The fix is one line in `generate_profiles_yml` (add `"search_path": schema_name`) or schema-qualify the generated SQL.

**Complexity.** accidental.

---

### F2 — Transform assets execute arbitrary user SQL with the managed-DB superuser role; no readonly isolation, cross-tenant read/exfiltration possible (LATENT / security, strong-inference)

**Mechanism.** Unlike the `query` MCP tool — which validates SQL via `sql_validator`, `SET ROLE <readonly>` and `SET search_path` to a single schema (`mcp_server/services/query.py:44-77`) — the transform executor runs raw asset SQL via dbt as the **full `MANAGED_DATABASE_URL` role** with **no role downgrade and no schema restriction**:

- `apps/transformations/services/executor.py:134-141` reads `settings.MANAGED_DATABASE_URL` and hands it to `generate_profiles_yml`; dbt connects as that superuser.
- `TransformationAsset.sql_content` is free-text. `save()`/`full_clean()` validate only `name` (the dbt-model RegexValidator); **`sql_content` is never validated** (`apps/transformations/models.py:100-114`, `serializers.py` has no sql validation).
- Because there is no `search_path` confinement (F1) and full privileges, an asset whose body is `SELECT * FROM t_<victim>.raw_cases` materializes the victim's data into a table **in the attacker's own tenant schema**, which the attacker then reads normally through the readonly query path. dbt's Jinja is also un-sandboxed against `{% do run_query('DROP SCHEMA … CASCADE') %}`, enabling DDL with superuser rights.

**Reachability.** Two routes, both live:
1. **Automatic** — a `scope=TENANT` asset created by any tenant member runs on the *next* materialization, because `_run_transform_phase` executes the system **and tenant** stages (`executor.py:50-69`). No manual trigger needed.
2. **Manual** — `POST /api/transformations/runs/trigger/` (`apps/transformations/views.py:121-166`, `IsAuthenticated`, tenant-membership gated).

Asset creation is `POST /api/transformations/assets/` (`views.py:54-59`), gated to the user's own tenant/workspace by `_check_write_permission`. So this is an authenticated-insider escalation, not anonymous — but it defeats exactly the per-tenant role isolation that `TODO.md`'s security section still lists as unchecked. The victim schema name is derivable from the victim's `external_id` (CommCare domain / Connect opportunity id) via `_sanitize_schema_name`, which is often guessable.

**Confidence.** strong-inference. The privilege facts (superuser DSN, no SET ROLE, unvalidated SQL) are verified-by-trace; I did not run an end-to-end exploit. Note F1 does **not** mitigate this — an attacker writes schema-qualified SQL that doesn't depend on search_path.

**Complexity.** essential risk of "run user dbt", but the missing role-downgrade is accidental given the query path already models the safe pattern.

---

### F3 — Workspace-scope transforms never run in the materialization flow (DEBT / correctness, verified-by-trace)

`_run_transform_phase` calls the pipeline **without a workspace**:

- `mcp_server/services/materializer.py:1062-1065` → `run_transformation_pipeline(tenant=tenant, schema_name=schema_name)` (no `workspace=`).
- The executor only appends the workspace stage when `workspace` is truthy (`apps/transformations/services/executor.py:62-69`).

So `scope=WORKSPACE` assets are executed **only** by the manual `/runs/trigger/` endpoint with an explicit `workspace_id` (`views.py:148-163`). That endpoint has **no frontend** (zero UI references), so in the shipped product, workspace-scope transformation assets are write-only data — creatable via API, never materialized. The model, scope enum, unique constraint, and executor stage for workspace assets are all built but unreachable in practice.

**Complexity.** accidental.

---

### F4 — Agent's system prompt and its actual tools disagree about which tables exist (LATENT / correctness, verified-by-trace)

There are divergent table-listing implementations and the agent sees both:

- **System-prompt schema context** is transformation-aware: `apps/agents/graph/base.py:246-249` calls `transformation_aware_list_tables`, which adds terminal `TransformationAsset` names (`stg_case_*`, `stg_form_*`) and appends "Use the `get_lineage` tool…" (`graph/base.py:276-280`, `metadata.py:269-325`).
- **The `list_tables`/`describe_table` MCP tools the agent calls** are **not** transformation-aware: `mcp_server/server.py:161` uses `pipeline_list_tables` (single-tenant) and `:131` uses `workspace_list_tables` (multi-tenant). `pipeline_list_tables` only surfaces declared pipeline sources plus `pipeline_config.dbt_models` (`metadata.py:79-110`) — and **every shipped pipeline declares zero `dbt_models`** (`pipelines/commcare_sync.yml` has no `dbt_models:` key; connect/ocs likewise). The dynamically generated staging models are never in that list.

Net: the prompt tells the agent that `stg_case_foo` exists and to explore it, but `list_tables` returns only `raw_cases`/`raw_forms`. Given the documented panic-loop class (#190 — agent destabilises on contradictory schema responses), this prompt-vs-tool drift is precisely the failure mode that breaker was added for.

`transformation_aware_list_tables` also lists assets **without verifying the physical table exists** — it stamps `materialized_row_count=None` and never checks `information_schema` (`metadata.py:313-323`), unlike `pipeline_list_tables` which reconciles against `_live_tables_in_schema` (`metadata.py:76,83,96-100`). So when F1 leaves the `stg_*` tables non-existent, the prompt still advertises them.

**Complexity.** accidental (two listing code paths solving the same problem differently — the "same problem solved N ways" pattern).

---

### F5 — `upsert_system_assets` never deletes orphaned assets; stale staging models accumulate and keep executing (LATENT / correctness, verified-by-trace)

`upsert_system_assets` only `update_or_create`s per generated asset (`apps/transformations/services/commcare_staging.py:333-347`); it never deletes assets that the latest metadata no longer produces. When a CommCare form/case-type is renamed or removed, its old `stg_*` asset (keyed on the old slug `name`) survives. On the next materialization:

- the orphan is still selected by the system stage (`executor.py:73` `TransformationAsset.objects.filter(tenant=…, scope=SYSTEM)`) and handed to dbt,
- it is still listed to the agent by `transformation_aware_list_tables` (F4),
- if the underlying raw columns changed it fails (isolated/silent, F1/F7); if it still compiles it produces a **stale** table the agent may query.

This is the same renamed-form-slug churn that drove `c400db4`/`69672ac` (slugify-from-dict crashes) — the slug is metadata-derived and not stable across app edits, so orphans are expected over a tenant's lifetime.

**Complexity.** accidental.

---

### F6 — Transform failures are doubly swallowed and never reach the user (DEBT / observability, verified-by-trace)

Beyond the executor's own isolation (`executor.py:85-92`, "Don't re-raise"), the materializer catches the transform exception and continues to COMPLETED (`materializer.py:455-457`, `:471-477`), and the resume aggregator that builds the agent's post-materialization message reads only `result["sources"]`, ignoring `result["transforms"]` entirely (`apps/workspaces/tasks.py:961-982`). The `transform_error` field added at `materializer.py:506-507` is therefore dropped before it can reach a human. A tenant whose every staging model fails is told "materialization completed." This is the documented "failures swallowed → agent told completed" class (the view-schema variant was fixed in PR #229; the transform variant was not).

**Complexity.** accidental.

---

### F7 — Entire dbt-execution path has zero non-mocked test coverage (DEBT / velocity, verified-by-trace)

`tests/test_transformation_executor.py` patches `run_dbt` + `generate_profiles_yml` in all 14 tests; `tests/test_dbt_runner.py` patches `dbtRunner` in all `TestRunDbt` cases. No test ever runs dbt against a real Postgres with the generated staging SQL. The result is that F1 (a fatal, one-line-fixable bug on the live path) and F4/F5 (catalog drift) cannot be caught by CI. This is the v1-methodology "what do the mocks hide" pattern in its purest form: the seam between "generate SQL" and "dbt runs SQL against a real schema" has no coverage at all.

**Complexity.** accidental.

---

### F8 — `/runs/trigger/` runs dbt synchronously inside the request thread (DEBT / cost-perf, verified-by-trace)

`TransformationRunViewSet.trigger` (`apps/transformations/views.py:121-166`) is a synchronous DRF action that calls `run_transformation_pipeline` inline, which invokes dbt under a process-global `threading.Lock` (`mcp_server/services/dbt_runner.py:25,96,143`). A multi-model run blocks the serving thread for the full dbt duration and serialises with any concurrent dbt in the same process. Low impact today (no UI, rarely hit), but it is an HTTP endpoint that can hold a worker for the length of a dbt build. Separately, the lock is **per-process**, so a worker-side materialization transform and an API-side `/trigger/` against the same tenant schema are **not** mutually serialised — concurrent `DROP/CREATE TABLE` DDL on the shared `t_<id>` schema can race (LATENT edge).

**Complexity.** accidental.

---

### F9 — Inline import in the hot schema-context path (COSMETIC / velocity, verified-by-trace)

`apps/agents/graph/base.py:244` imports `aget_terminal_assets` inside the function body, violating the repo's module-level-imports rule. Commit `27897fc` ("move inline imports to module level") swept the backend but missed/regressed this one. Pure style/debt; flagged because the cartography lists import discipline as a maintained invariant.

---

## What's actually fine

- **Lineage cross-tenant scoping** — both `get_lineage_chain`/`aget_lineage_chain` and `transformation_aware_list_tables` traverse the `replaces` chain through a visibility filter (`tenant_id__in` OR `workspace_id`), preventing cross-tenant FK disclosure (`apps/transformations/services/lineage.py:50-82,108-135`; `metadata.py:289-303`). This was a deliberate fix (`a19ce46`) and it holds.
- **Lineage cycle guard** — `visited` set bounds the `replaces` walk (`lineage.py:67,121`).
- **Terminal/"most-downstream" tie-break** — `order_by("-scope")` correctly prefers `workspace` > `tenant` > `system` (string ordering aligns with the hierarchy); comment matches behavior (`lineage.py:60-62`).
- **Generated-SQL injection surface** — single quotes are doubled (`_sql_escape`), question paths use the `ARRAY[...]::text[]` constructor rather than brace literals, column aliases and model names are slugified, and `name` is RegexValidator-constrained. With Postgres' default `standard_conforming_strings`, this is sound against the metadata-derived inputs (`commcare_staging.py:68-103,140-145`). (Note: this protects the *auto-generated* SQL; it does nothing for user-supplied `sql_content` — see F2.)
- **Model integrity constraints** — `transformation_asset_one_container` CheckConstraint plus the two partial UniqueConstraints correctly enforce exactly-one-container and per-scope name uniqueness; `clean()` mirrors them (`models.py:74-114`).
- **AssetRun orphan cleanup** — a stage that raises before recording marks still-RUNNING asset runs FAILED (`executor.py:107-118`, fix `ef37760`).
- **Cancellation CAS around the transform phase** — the LOADING→TRANSFORMING and TRANSFORMING→COMPLETED transitions are compare-and-swap, preserving an externally-set CANCELLED/FAILED state (`materializer.py:435-443,471-483`).
- **dbt intra-process serialisation** — the module-level `_dbt_lock` correctly serialises concurrent in-process dbt invocations (the worker's per-tenant loop and concurrent procrastinate tasks), matching dbt's documented non-thread-safety (`dbt_runner.py:25,96,143`).

---

## Coverage log

**Deep-read (line-by-line):**
- `apps/transformations/models.py`, `views.py`, `serializers.py`, `urls.py`
- `apps/transformations/services/executor.py`, `dbt_project.py`, `lineage.py`, `commcare_staging.py`
- `mcp_server/services/dbt_runner.py`
- `mcp_server/services/materializer.py` transform integration (lines ~120-509, ~1060-1075)
- `apps/workspaces/services/schema_manager.py` (view-schema build, full file)
- `mcp_server/services/metadata.py` listing functions (lines ~55-360)
- `apps/agents/graph/base.py` schema-context assembly (lines ~225-394)
- `mcp_server/server.py` `list_tables` + `get_lineage` (lines ~108-330, ~521-640)
- `tests/test_transformation_executor.py`, `tests/test_dbt_runner.py`
- `pipelines/commcare_sync.yml`
- dbt-postgres adapter `connections.py` + `create.sql` macros (search_path verification)

**Skimmed:**
- `apps/workspaces/tasks.py` `materialize_workspace`, `_aggregate_materialization_state`, sibling-rebuild helpers (read the transform-relevant sections, not the full 1,289 lines)
- `mcp_server/services/materializer.py` table-writer functions below line 1075 (read enough to confirm raw-table DDL shape for F1)
- frontend grep sweep (confirmed zero transformation UI)

**NOT examined (gap-loop candidates):**
- `tests/test_commcare_staging_generator.py`, `test_dbt_project_writer.py`, `test_lineage.py`, `test_transformation_api.py`, `test_transformation_models.py` — did not read; I verified executor/runner mocking but not whether these assert anything about real SQL shape or the F1/F5 behaviors.
- `apps/transformations/migrations/0001_initial.py`, `0002_add_name_validator.py` — not opened; did not check whether existing rows could violate the M7 RegexValidator.
- `apps/transformations/services/lineage.py` async vs sync **duplication** — noted both exist but did not audit for drift between the two copies beyond confirming they mirror.
- `pipelines/connect_sync.yml` / `ocs_sync.yml` full contents — only grepped for `dbt/model/transform` (zero hits); did not read in full.
- `mcp_server/services/metadata.py` `pipeline_describe_table` behavior when handed a transformation-model name that does/doesn't physically exist — inferred, not traced.
- The legacy `refresh_tenant_schema` path's transform behavior (`tasks.py:173` also calls `run_pipeline`) — out of this vertical's core; flagged for the materialization vertical.
- Whether dbt's `{% do run_query %}` is actually reachable in this dbt version's model context (F2 jinja vector) — asserted from general dbt behavior, not verified against the pinned version.
- Runtime confirmation of F1 against a live Postgres — explicitly not run (read-only review).
