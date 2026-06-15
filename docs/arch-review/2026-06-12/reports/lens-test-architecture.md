# Lens: Test Architecture — what the mocks hide

*Reviewer: cross-cutting lens 7 (test architecture). 2026-06-12.*
*Mandate: one defect class, hunted everywhere — mocks that conceal broken contracts,
inter-process seams with zero unmocked coverage, and tests that encode contracts the
implementation no longer honors.*

The test suite is the largest body of code in the repo (~27,000 LOC, test:app ≈ 1.4:1).
Its *shape* is the finding: nearly all of that mass sits inside single-process unit
tests with the process boundaries mocked out, while the incidents of the last month
all lived **on** the process boundaries. Worse, the handful of tests that do cross a
real boundary (real managed-DB DDL, real writer SQL) are gated behind an environment
variable that CI never sets — including the regression tests written for the
2026-06-10 production incident.

---

## Finding T1 — CI never runs the 2026-06-10 incident regression tests (silent module-level skip)

**Status: BROKEN-NOW · Impact: correctness (of the regression guard) · Confidence: verified-by-trace · Complexity: accidental**

The regression tests added by the post-incident PRs #227 ("truncation-safe view names
and idempotent view schema rebuild", `2aaf4fb`) and #229 ("surface view-schema build
failures", `ef42b4e`) live in `tests/test_view_schema_builder.py`. That module has a
**module-level** skip marker:

```python
# tests/test_view_schema_builder.py:19-22
pytestmark = pytest.mark.skipif(
    not os.environ.get("MANAGED_DATABASE_URL"),
    reason="MANAGED_DATABASE_URL not set",
)
```

A module-level `pytestmark` applies to *every* test in the file — including the four
tests in that module that are fully mocked and need no managed DB at all
(`test_build_view_schema_bulk_fetches_tenant_schemas` :354,
`test_build_view_schema_returns_active_record` :395,
`test_build_view_schema_clears_last_error_on_success` :725,
`test_build_view_schema_records_last_error_on_failure` :761 — the #229 regressions).

The CI test job sets only `DATABASE_USER/PASSWORD/HOST/PORT`, `TEST_DATABASE_NAME`,
`DJANGO_SECRET_KEY`, `DJANGO_SETTINGS_MODULE` (`.github/workflows/ci.yml`, `test:` job
`env:` block). **No workflow in `.github/workflows/` sets `MANAGED_DATABASE_URL`**
(verified by grep across all workflows). `.env.example` does not mention it either —
only the local `.env` defines it, so a fresh checkout skips these tests locally too.

Executed demonstration (simulating CI's env):

```
$ MANAGED_DATABASE_URL= uv run pytest tests/test_view_schema_builder.py tests/test_ocs_materializer.py -q -rs
...
============================= 18 skipped in 0.15s ==============================
```

18 tests skip: all 13 in `test_view_schema_builder.py` (including the
`test_build_view_schema_long_canonical_name_no_truncation_collision` :448 and
`test_build_view_schema_two_long_names_shared_head_get_distinct_prefixes` :527
regressions for the exact 63-byte truncation collision that broke production) and all
5 OCS writer tests in `test_ocs_materializer.py` (same module-level marker at :23).

**Chain**: `.github/workflows/ci.yml` test job env (no `MANAGED_DATABASE_URL`) →
`uv run pytest` → `tests/test_view_schema_builder.py:19` module skip →
0 of 13 tests execute → the incident-class regressions are unguarded on every PR.

**Consequence**: a reintroduction of the truncation-collision bug, the
swallowed-build-failure bug, or any OCS writer regression merges green. The tests
exist, were written deliberately as part of the incident response, and have never
run in CI. git history confirms the file has carried the module skip since long
before the incident commits were added to it (`965a407` → `2aaf4fb` → `ef42b4e`).

**Fix shape** (for the synthesis phase, not applied here): split the mocked tests out
of the skip scope; add a postgres service schema + `MANAGED_DATABASE_URL` to the CI
job (the CI postgres container can serve both roles); make the skip loud (a CI step
that fails if the skip count for this marker is nonzero).

---

## Finding T2 — Connect/CommCare writer data-integrity regressions also never run in CI

**Status: BROKEN-NOW · Impact: correctness (guard) / data-loss (guarded class) · Confidence: verified-by-trace · Complexity: accidental**

The regression tests for the Connect silent row-duplication bug (`f26c1a0`) and the
missing-id NotNullViolation (`2587158`) are real-database writer tests in
`tests/test_materializer.py` (`TestConnectIdlessWriters`, :1174 onward; CommCare
`_write_cases`/`_write_forms` real-DB tests at :1080/:1139). They gate on:

```python
# tests/test_materializer.py:1090, 1190
db_url = os.environ.get("MANAGED_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not db_url:
    pytest.skip("No MANAGED_DATABASE_URL/DATABASE_URL for writer test")
```

CI sets **neither** variable — `config/settings/test.py` documents this explicitly:
*"CI: sets DATABASE_USER/PASSWORD/HOST/PORT explicitly (no DATABASE_URL)."* So the
duplication regressions for `_write_connect_payments`, `_write_connect_completed_works`,
`_write_connect_visits` (page replay) and both CommCare writers skip on every PR.

Two sub-points:

1. The symptom seed said "siblings among the other ~14 loaders never audited" — the
   OCS writer tests *were* written (`test_ocs_materializer.py`) but are dead in CI
   (Finding T1), so from CI's point of view all three providers' writer layers are
   equally unverified.
2. Locally these tests fall back to `DATABASE_URL` — the **platform** dev database —
   and create/drop schemas like `test_cpr_payments` in it (`_make_schema` does
   `DROP SCHEMA ... CASCADE`). Tests that mutate the developer's platform DB as a
   fallback are themselves a hazard, and an incentive not to run them.

---

## Finding T3 — The refresh tests mock the exact seam where the data loss occurs, then pin the destructive contract

**Status: BROKEN-NOW · Impact: data-loss · Confidence: verified-by-trace · Complexity: accidental**

This replicates the v1 run-A S1 finding from the test-architecture side, with the
mechanism fully traced. The point this lens adds: **the test suite doesn't just miss
this bug — it asserts the destructive half of it.**

The production chain:

1. **Entry**: Data Dictionary refresh —
   `frontend/src/store/dictionarySlice.ts:197` → `await api.post(`/api/workspaces/${activeDomainId}/refresh/`)`
   → route `apps/workspaces/api/urls.py:22` (`RefreshSchemaView`).
2. The view defers `refresh_tenant_schema` (`apps/workspaces/tasks.py:126`), which
   creates a **new** TenantSchema row with a suffixed name (test fixture shape:
   `test_domain_r12345678`) and creates that physical schema
   (`tasks.py:150` `manager.create_physical_schema(new_schema)`).
3. It then calls the pipeline **without telling it which schema to load**:
   `tasks.py:173` → `run_pipeline(membership, credential, pipeline_config)`.
4. `run_pipeline` picks its *own* target:
   `mcp_server/services/materializer.py:183` → `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`.
5. `provision()` resolves the schema name from the tenant, not from the refresh:
   `apps/workspaces/services/schema_manager.py:66` →
   `schema_name = self._sanitize_schema_name(tenant.external_id)` — i.e. the **old**
   schema (`test_domain`), which it finds ACTIVE and returns (:68-78). All freshly
   loaded rows land in the old schema.
6. Back in the task: the **empty** new schema is marked ACTIVE
   (`tasks.py:182-184`), and old ACTIVE schemas — now containing the fresh data —
   are moved to TEARDOWN and scheduled for destruction (docstring at `tasks.py:129`:
   *"On success: marks state=ACTIVE, schedules teardown of old active schemas"*).

Now the test architecture. `tests/test_refresh_task.py`:

- Every test patches `apps.workspaces.tasks.run_pipeline` (e.g. :73, :108) — the
  exact call whose schema-targeting is the bug — plus
  `schema_manager.get_managed_db_connection` and `aresolve_credential`. The
  cross-component contract "the pipeline loads into the schema the refresh created"
  is therefore *unobservable* by construction.
- `test_refresh_task_schedules_old_schema_teardown` (:88-120) then **asserts** that
  teardown of the old ACTIVE schema is scheduled. With the loader mocked, this test
  pins precisely the half of the behavior that destroys the data. It will pass
  forever, including against the broken implementation; it would *fail* against a
  naive fix that stops tearing the old schema down.

`tests/test_refresh_endpooint.py`-style view tests patch
`refresh_tenant_schema.defer` (3 occurrences per mock census), so the HTTP→task seam
is also never crossed.

**Reachability**: live — `dictionarySlice.ts:197` is wired to the data-dictionary UI.

---

## Finding T4 — The chat ↔ MCP wire has zero unmocked coverage; "integration" tests are single-process

**Status: DEBT · Impact: correctness + velocity · Confidence: verified-by-trace · Complexity: accidental**

Census of `mock.patch` targets across `tests/` (top entries):

```
17 mcp_server.services.materializer.MaterializationRun
16 mcp_server.services.materializer.get_managed_db_connection
13 apps.workspaces.tasks.SchemaManager
12 apps.workspaces.tasks.aresolve_credential
 7 apps.chat.views.get_mcp_tools
 7 apps.chat.views.build_agent_graph
 2 apps.agents.mcp_client.MultiServerMCPClient
```

What this shape means, seam by seam:

- **Agent → MCP transport**: `tests/test_mcp_client.py` patches
  `MultiServerMCPClient` itself — the only code in the repo that speaks the MCP
  protocol is tested against a mock of the protocol library.
- **Chat → agent → tools**: `tests/test_mcp_chat_integration.py` (974 lines,
  docstring: *"MCP server: mocked via get_mcp_tools() returning fake LangChain
  tools"*) — the fake tools are hand-written in the test, so their schemas can drift
  arbitrarily from `mcp_server/server.py` without any test noticing.
- **MCP server side**: `tests/test_mcp_server.py` (docstring: *"Database access is
  mocked at the Django ORM / psycopg boundary"*) and `tests/test_mcp_tenant_tools.py`
  call tool handler functions **in-process**; FastMCP itself (serialization, the
  envelope over the wire, progress notifications, `tool_context` resolution under a
  real request) is never started in any test.

No test anywhere starts the FastMCP server and connects `apps/agents/mcp_client.py`
to it. The cartography's churn data shows the chat↔MCP↔worker spine is where ~all
fix commits concentrate (`server.py` 36 fix-touches, `graph/base.py` 27,
`chat/views.py` 27, `tasks.py` 24) — the highest-churn seam in the system is the one
seam the 27,000-line suite cannot see.

This is the same structural blindness that already bit once: per project memory, the
`aresolve_credential` mock hid a `SynchronousOnlyOperation` crash in the refresh task
(fixed `8104ce1`) — the test suite passed while the production code path raised on
its first real execution. The 12 remaining `apps.workspaces.tasks.aresolve_credential`
patches keep that masking pattern alive for task bodies (mitigated, but not removed,
by the fact that the resolver itself now has real async tests in
`tests/test_ocs_connections.py:147-414`).

---

## Finding T5 — `MCP_TOOL_NAMES` membership is a cross-process contract enforced only by hand, and the test for it is circular

**Status: LATENT · Impact: correctness (potential security-adjacent) · Confidence: strong-inference · Complexity: accidental**

`apps/agents/graph/base.py:65` hard-codes the set of MCP tools that receive
workspace-context injection and have their sensitive params hidden from the LLM:

```python
MCP_TOOL_NAMES = frozenset({"list_tables", "describe_table", "query", "get_metadata",
    "run_materialization", "get_schema_status", "teardown_schema", "get_lineage"})
```

Two consumers: `base.py:408` (tools *not* in the set keep their **raw schema**,
including any `workspace_id` param, visible to the LLM) and `base.py:460` (tool calls
not in the set get **no injected** `workspace_id`/`tool_call_id`).

The only test of this contract is `tests/test_agent_graph.py:7-18`, which asserts
that specific literal names are present **in the constant itself** — both sides of
the assertion are the same hand-maintained frozenset. No test imports the actual
tool registry from `mcp_server/server.py` and checks that every tool taking a
`workspace_id` parameter is a member. The server currently exposes 11 tools; the 3
absent from the set (`list_pipelines`, `get_materialization_status`,
`cancel_materialization` — `server.py:379/:408/:446`) happen not to take
`workspace_id` today, so the contract holds — by coincidence of manual upkeep, not
by any guard. The failure mode when the next tenant-scoped tool is added on the
server side only: the LLM sees and fills `workspace_id` itself (hallucinated or
copied from context), and injection/attribution (`tool_call_id`, `base.py:460-470`)
silently doesn't happen. Given the MCP server's trust-the-caller authz model, an
LLM-supplied `workspace_id` is accepted.

This is the same drift class as the prompt↔validator drift the team already fixed
once (`93504d5`), one seam over.

---

## Finding T6 — Frontend: no unit-test infrastructure at all; e2e exists but runs nowhere; the #231 bug class has no regression test

**Status: DEBT · Impact: velocity / correctness · Confidence: verified-by-trace · Complexity: accidental**

- `frontend/package.json` has no test runner (no vitest/jest); the only test scripts
  are Playwright e2e (`test:e2e`, `test:e2e:widget`, `test:e2e:integration`).
- `frontend/tests/e2e/` holds 4 specs (`widget-sdk`, `embed-integration`,
  `connect-tenant`, `labs-smoke`); `labs-smoke` is `headless: false` (interactive),
  `connect-tenant` needs live credentials. **CI runs none of them** — `ci.yml`'s
  frontend job is ESLint only.
- The 2026-06-10 incident item (c) — threadId carried across workspace switches —
  was fixed in `00c423d` in `frontend/src/hooks/useWorkspaceThreadSync.ts` plus
  localStorage handling. There is no test runner that *could* host a regression test
  for that hook, and no e2e covers workspace switching. The bug class (client-side
  state leaking across workspace context) has zero automated coverage and already
  produced one production incident.
- 13,100 LOC of frontend, including the hand-written TS mirrors of API shapes
  (`frontend/src/api/*.ts` — seam 4 in the cartography), are verified only by `tsc`
  against themselves.

---

## Finding T7 — Tenant isolation (readonly role / SET ROLE) is never tested against a real database in CI

**Status: LATENT · Impact: security · Confidence: verified-by-trace · Complexity: accidental**

The query path's isolation mechanism is `SET ROLE <schema>_ro` + `search_path`
(`mcp_server/services/query.py`). Its tests, `tests/test_query_role_isolation.py`,
patch `psycopg.AsyncConnection.connect` with a MagicMock connection (:55-60) and then
assert that the *strings* `SET ROLE ...` were executed on the mock cursor. Whether
the role exists, whether the GRANTs actually confine it to the tenant schema, whether
`RESET ROLE` ordering is right under error paths — none of that is observable on a
mock.

The single test that checks real grant behavior,
`test_build_view_schema_readonly_role_has_access`
(`tests/test_view_schema_builder.py:292`), is inside the module skipped in CI
(Finding T1). Net: the platform's primary tenant-isolation enforcement has zero
CI-executed coverage against a real PostgreSQL, in a codebase whose own TODO.md
still lists "per-tenant PostgreSQL role isolation" as an unchecked security item.

---

## Finding T8 — Post-deploy verification exists as code but is wired to nothing

**Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

`tests/smoke/test_deployment.py` (pure-HTTP deployment smoke checks) and
`tests/smoke/test_connect_sync.py` are excluded by default
(`pyproject.toml:97` `addopts = "-v --tb=short -m 'not smoke'"`) — correct for unit
CI — but neither `deploy.yml` nor `deploy-labs.yml` contains any pytest/playwright
step (verified by grep), so nothing runs them after a deploy. Both June incidents
were detected by humans noticing stuck UI; an automated post-deploy smoke run is
already written and just not invoked.

---

## Cross-cutting observation: the suite's center of mass is one process deep

Essential vs accidental: a large unit suite with mocked boundaries is *essential*
for a multi-process system — you cannot run CommCare/Connect/OCS in CI. What is
*accidental* is that the compensating layers (real-DB writer tests, view-schema DDL
tests, e2e, smoke) all exist in the repo and all sit behind gates that no automated
environment opens: `MANAGED_DATABASE_URL` (T1/T2), live credentials (e2e), manual
invocation (smoke). The team keeps writing exactly the right regression tests after
each incident — and the harness keeps filing them where CI can't see them. The next
incident's postmortem will likely again say "a test existed."

## What's fine (verified healthy)

- **`tests/test_worker_db_resilience.py`** — exemplary: kills the real psycopg
  connection behind Django's back, asserts recovery through the real decorator, and
  includes a meta-test (`test_all_workspace_tasks_are_wrapped`) that fails if any
  future task bypasses `config.procrastinate.task`. This is contract-pinning done
  right, and it runs in CI.
- **TTL-janitor regression coverage runs in CI** — `tests/test_schema_ttl_task.py:56`
  pins "provision() now refreshes last_accessed_at" (incident item b) using ORM-only
  fixtures, so unlike T1 this 2026-06-10 regression *is* guarded.
- **ThreadJob janitor tests insert real `procrastinate_jobs` rows** (factory at
  `tests/test_threadjob_janitor.py:25-55`) and test `_procrastinate_job_status`
  directly against a real ORM row (:354) — the procrastinate-table seam has genuine
  partial coverage; mocking it in the behavior tests above that layer is legitimate
  layering.
- **Prompt-pinning tests** (`tests/agents/test_panic_loop_eval.py`,
  `test_metadata_provenance_eval.py`) — honest about CI having no LLM key; they pin
  the prompt *wording* and the assembled prompt content so prompt↔code drift (a
  recurring incident class here) breaks a test.
- **CI tests run against real PostgreSQL 16**, not sqlite (`config/settings/test.py`)
  — DB-specific behavior (JSONB, identifiers) is at least in play for the ORM layer.
- **Migration archaeology test** — `tests/test_ocs_connections.py:85-97` recreates
  the dropped `TenantCredential` model via the historical app registry to test the
  0007 data migration; the 3 `TenantCredential` references in tests are deliberate,
  not residue.
- **The resume/ThreadJob suite** (`tests/test_resume_thread_task.py`, 1,016 lines)
  covers the CAS claim races, double-dispatch, cancellation flips, and LLM-failure
  paths exhaustively at the unit level (mock census: 19 mock agents, 18 graph
  patches) — within its single-process scope it is thorough.

## Coverage log

**Deep-read** (line-level, with execution where noted):
`tests/conftest.py`; `tests/test_view_schema_builder.py` (structure + skip scope; executed skip demo); `tests/test_ocs_materializer.py` (skip scope); `tests/test_refresh_task.py` (first 120 lines + mock census); `tests/test_worker_db_resilience.py` (full); `tests/test_materialize_workspace_task.py` (first 80 + defer census); `tests/test_mcp_chat_integration.py` (first 120 + docstring); `tests/test_query_role_isolation.py` (first 60); `tests/test_threadjob_janitor.py` (structure, factory, :354 test); `tests/agents/test_panic_loop_eval.py` (first 40); `.github/workflows/ci.yml` (lint+test jobs); `config/settings/test.py`; `pyproject.toml` pytest config; `apps/workspaces/tasks.py:126-184, 690-727`; `mcp_server/services/materializer.py:96-210`; `apps/workspaces/services/schema_manager.py:57-117`; `apps/agents/graph/base.py:65-90, 395-470`; `mcp_server/server.py` (signatures of the 3 non-member tools); `frontend/playwright.config.ts`; `frontend/package.json`; mock.patch census across all of `tests/` (mechanical grep, full).

**Skimmed**: `tests/test_materializer.py` (cursor/duplication test sections :845-1215); `tests/test_mcp_server.py` (head); `tests/test_mcp_client.py` (head); `tests/test_schema_ttl_task.py` (last_accessed_at greps); `tests/test_agent_graph.py` / `test_agent_graph_injection.py` (MCP_TOOL_NAMES greps); `tests/test_workspace_permissions.py` (head — note: permission classes ARE imported by 4 view modules, so the v1 "dead code" claim deserves separate adjudication, not done here); `tests/smoke/test_deployment.py` (head); `tests/test_ocs_connections.py` (aresolve + migration greps); `tests/agents/test_metadata_provenance_eval.py` (greps); `mcp_server/auth.py` (head); `.github/workflows/deploy*.yml`, `claude.yml` (greps only); `tests/test_commcare_forms_loader.py` (dedup grep only).

**Not examined** (in-scope for this lens, honestly untouched):
bodies of `tests/test_resume_thread_task.py` (only its mock census), `tests/test_recipes.py`, `tests/test_auth.py`, `tests/test_artifacts.py`, `tests/test_export.py`, `tests/test_jobs_endpoints.py` (788 lines), `tests/test_mcp_tenant_tools.py` (989 lines), `tests/test_metadata_service.py`, `tests/test_sql_validator.py` (whether its cases match the current prompt's claims — the drift-test gap is inferred, not proven, for this pair), all loader test bodies (`test_connect_*`, `test_ocs_*loader*`, `test_commcare_*` beyond greps), `tests/test_merge_users_service.py`, `tests/test_social_login_reconciliation.py`, `tests/test_tenant_*` family, `tests/test_transformation_*`, `tests/test_knowledge_*`, `tests/test_schema_manager.py` beyond its mock census, `tests/test_multitenant_smoke.py`, `tests/test_dangling_tool_calls.py`, `tests/agents/test_schema_context.py`, `tests/agents/test_tracing.py`, `tests/qa/data-dictionary-scenario.md` content, `tests/smoke/test_connect_sync.py`, the 4 frontend e2e spec contents, and whether `uv run pytest` skip counts surface anywhere in PR review practice.
