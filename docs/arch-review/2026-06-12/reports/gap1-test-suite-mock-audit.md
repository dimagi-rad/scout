# Gap Round 1: Test-Suite Mock Audit

Reviewer: gap-1 subagent (test-body deep read). Date: 2026-06-12. HEAD: 35e4230.

Mandate: read the bodies of the large mock-heavy suites the test-architecture lens only
censused, and adjudicate what production behavior is actually pinned versus mocked away.
Per suite: seams mocked, known-finding bug classes the suite could NOT catch, and tests
that pin currently-broken behavior as correct.

Known findings from earlier rounds are referenced, not re-reported. New findings are
marked **[NEW]** and carried into structured output.

---

## 1. tests/test_resume_thread_task.py (1,016 LOC)

**Verdict: far better than its mock census suggests.** This is a real-DB state-machine
suite, not a mock theater.

### Seams mocked
- `apps.workspaces.tasks._build_agent_for_resume` → `AsyncMock(return_value=(mock_agent, {}))`.
  The entire LangGraph agent, checkpointer, and MCP wire are behind this one seam.
- `apps.workspaces.tasks._resume_langfuse_span` (observability no-op).
- In the timeout/exception paths, `aupdate_state` on the mock agent.

Everything else is real: User/Workspace/Tenant/TenantSchema/Thread/ThreadJob/
MaterializationRun rows created via async ORM with `django_db(transaction=True)`, and the
task's CAS writes land in the actual test database.

### What is genuinely pinned (real CAS, not mocked)
- `test_resume_does_not_clobber_concurrent_cancel_during_ainvoke`: the mock agent's
  `side_effect` flips the ThreadJob row to CANCELLED *mid-ainvoke* in the DB, then the
  test asserts the task's filtered `aupdate` CAS does not clobber CANCELLED back to a
  terminal resume state. This is a true DB-level pin of the cancel-vs-resume race
  resolution — the strongest concurrency test in the repo.
- `test_resume_cas_rejects_already_running_threadjob`: precondition-style (row pre-set to
  RUNNING before the task starts), so it pins the CAS guard but is not a true concurrent
  race.
- Terminal-state mapping: partial materialization → FAILED with error_summary; cancelled
  ThreadJob with completed runs → COMPLETED; view-schema build failure (real
  WorkspaceViewSchema rows in FAILED state) surfaced into the system message — the
  2026-06-10 incident regressions are pinned here with real rows, and these DO run in CI
  (no managed-DB skip on this file).

### Bug classes this suite cannot catch
- ThreadJob create-after-defer race (tasks.py:366–374 TODO) — the task under test starts
  after the rows exist.
- Resume-vs-live-chat serialization; real checkpointer state; anything downstream of
  `agent.ainvoke` (tool calls, MCP transport) — all behind the `_build_agent_for_resume`
  seam. The chat-to-MCP zero-unmocked-coverage finding stands; this suite does not touch
  the wire.

### Pins of currently-broken behavior
- **[NEW]** `test_resume_appends_system_message_and_invokes_agent` asserts
  `"oauth_tokens" in config` passed to the agent. Per the known finding, OAuth-token
  plumbing into MCP is dead end-to-end; this assertion codifies the dead plumbing as the
  contract, so removing or fixing the plumbing will look like a regression and the test
  green looks like working OAuth forwarding. (DEBT / velocity, verified-by-trace.)

---

## 2. tests/test_jobs_endpoints.py (788 LOC)

**Verdict: real HTTP + real DB integration, but it mocks exactly the two seams that have
bitten production, and the reconciler's false-failure branch is uncovered (not codified).**

### Seams mocked
- `apps.workspaces.tasks._procrastinate_job_status` → AsyncMock returning
  "failed"/"succeeded" strings. The real ProcrastinateJob ORM read (the function whose
  docstring narrates the FutureApp incident) is never executed in any reconcile test.
- `apps.workspaces.api.materialization_views.materialize_workspace.defer_async` and
  `resume_thread_after_materialization.defer_async` — no real defer round-trips.
- `apps.workspaces.api.jobs_cancel.current_app` → MagicMock with
  `job_manager.cancel_job_by_id_async`. **[NEW]** `jobs_cancel.py:11` does
  `from procrastinate.contrib.django.procrastinate_app import current_app` at module
  import time and calls `current_app.job_manager.cancel_job_by_id_async(...)` at line 44.
  This is one of the three known surviving FutureApp import-time-binding sibling sites —
  and the tests patch precisely that attribute, so the binding-class bug (current_app
  being a FutureApp shell when the module imports before app configuration) can never
  fire under test. The cancel tests verify the endpoint's DB writes and response shape,
  not that cancellation reaches Procrastinate.
- `_persist_synthetic_failure_message` (asserted called, body not executed).

### Reconciler adjudication (mandate question: pin or codify?)
**Neither — the dangerous branch is simply untested.** Every stale-reconcile test goes
through `_make_stale_pending_job`, i.e. ThreadJob state=PENDING only. The known
false-failure finding lives in the `tj.state == RUNNING` branch of
`reconcile_stale_thread_job` (tasks.py ~600–700): a healthy resume running >10 minutes is
flipped to FAILED because `tj.procrastinate_job_id` points at the *materialization* job,
whose status is already "succeeded". No test constructs state=RUNNING with a
succeeded-materialization job id, so the suite neither pins the false failure as correct
nor guards against it. **[NEW]** finding: zero coverage of the RUNNING reconcile branch.
Additionally, because `_procrastinate_job_status` is mocked everywhere, even the PENDING
tests validate the reconciler against an invented status oracle.

### What is genuinely pinned
- HTTP-level integration via AsyncClient: active-jobs payload shape, server-side percent
  computation, RECENT_TERMINATION_WINDOW filtering, membership 403s, per-poll API-side
  reconciliation triggering for old PENDING jobs.

### Other gaps
- All membership fixtures are READ_WRITE; zero role-distinction assertions on the jobs
  surface (consistent with the known WorkspaceRole-unenforced finding — these tests would
  pass identically whether roles were enforced or not).

---

## 3. tests/test_mcp_tenant_tools.py (989 LOC)

**Verdict: the tool-handler half is the weakest suite audited — Django model CLASSES are
patched as MagicMocks, so contract drift between server.py and the models is invisible.
The real-DB half (get_schema_status, execute integration) is good but partially CI-dead.**

### Seams mocked (tool-handler classes)
`patch("mcp_server.server.WorkspaceViewSchema")`, `TenantSchema`, `MaterializationRun`,
`Tenant`, `TenantMetadata` as MagicMock classes; plus `load_workspace_context`,
`pipeline_list_tables`, `pipeline_describe_table`, `pipeline_get_metadata` as functions.
MagicMock auto-attribute creation means any ORM keyword, attribute path, or manager chain
server.py uses (or misuses) is accepted silently.

### Concrete proof of the hazard: mock residue **[NEW]**
- Production: `mcp_server/server.py:426` and `:484` read
  `run.tenant_schema.tenant.external_id` (TenantSchema's FK is `tenant`,
  apps/workspaces/models.py:28).
- Tests: `tests/test_mcp_tenant_tools.py:759` and `:803` set
  `mock_run.tenant_schema.tenant_membership.tenant.external_id = "dimagi"` — an attribute
  path that does not exist on the real model (the `tenant_membership` OneToOne lives on
  TenantMetadata, models.py:266). The same residue appears on TenantSchema mocks at lines
  338/388/437 (`mock_ts.tenant_membership = MagicMock()`).
- The tests still pass because MagicMock fabricates `tenant.external_id` on demand. So
  the cancel/status tests assert nothing about which tenant id is used — the exact
  divergence class behind the known get_schema_status extinct-result-shape finding would
  also sail through here.

### Pins of currently-broken behavior
- `TestCancelMaterialization.test_cancel_in_progress_run` asserts only
  `result["data"]["cancelled"] is True`. Per the known finding, production writes FAILED
  (not CANCELLED) to the run. The test codifies the user-facing "cancelled: True" surface
  while never asserting the state written, so the FAILED-vs-CANCELLED bug is invisible
  and the test will pass before and after any fix — a non-pin masquerading as coverage.
  (Logged as a coverage-shape note under the existing finding, not re-reported.)

### Other uncatchable classes
- Multi-tenant list_tables branch: every test wires
  `mock_vs_cls.objects.filter.return_value.aexists = AsyncMock(return_value=False)` —
  only the single-tenant branch is ever exercised by the handler tests.
- teardown_schema: only confirm/workspace-id validation and not-found paths tested; the
  destructive DROP path is untested anywhere outside CI-skipped integration.
- Cross-tenant pg_catalog disclosure: out of scope for these mocks entirely.

### The good half
- `TestGetSchemaStatusTool` post-incident tests use real DB rows (not_provisioned, FAILED
  view schema with error text) and run in CI.
- `TestExecuteAsyncIntegration` exercises real SET ROLE / search_path /
  statement_timeout against PostgreSQL — but skips without DATABASE_URL (see §7).

---

## 4. tests/test_sql_validator.py (540 LOC) — drift question settled

**Verdict: prompt and validator are substantively ALIGNED post-93504d5; the residual
drift is one sentence; and the suite affirmatively pins the disclosure vector as
correct.**

- `apps/agents/prompts/base_system.py` now states accurately: "Unqualified `pg_catalog`
  views (`pg_namespace`, `pg_class`, `pg_views`, `pg_tables`) are reachable". That
  matches `mcp_server/services/sql_validator.py` behavior (schema check only on
  explicitly qualified tables; unqualified names ride search_path).
- Residual drift: the prompt's "**Schema-Scoped**: You can ONLY access tables within the
  current project's schema" contradicts the validator's
  `all_allowed_schemas = {"public", schema} | allowed_schemas` — `public.*` is always
  queryable. `test_reject_wrong_schema` pins `SELECT * FROM public.sensitive_data` as
  allowed when schema="analytics", with an in-test comment endorsing it. So the
  always-allowed-public behavior is deliberate and pinned; the prompt sentence is the
  stale artifact. (COSMETIC drift on top of the known disclosure finding.)
- **[NEW — pin of broken behavior]**
  `TestPromptValidatorAlignment.test_unqualified_pg_catalog_views_are_allowed` pins
  pg_tables/pg_views/pg_namespace/pg_class as ALLOWED. The known cross-tenant metadata
  disclosure finding flows through exactly this allowance. Any future fix (qualifying or
  blocking pg_catalog) must consciously flip this test; until then the suite certifies
  the vector as the contract.
- The "alignment" meta-tests are substring greps on the prompt text ("information_schema"
  present, "cannot query pg_catalog" absent, etc.) — brittle but currently truthful.
  They enforce phrase presence, not semantic alignment.

---

## 5. Loader suites — f26c1a0/2587158 adjudication

### tests/test_connect_data_loaders.py (462 LOC)
**Realistic, not hand-built.** Payloads explicitly mirror the upstream DRF serializers
(comments cite `commcare_connect.data_export.serializer`): FK ints, JSONField dicts,
DurationField "0:30:00", integer payment_accrued. Uses `requests_mock` at the HTTP seam —
the right seam for a loader. Covers pagination-follows-next, 401 → ConnectAuthError,
missing-results-key → ConnectExportError, Accept header, Bearer auth. Gap: only
same-host `next` URLs are exercised (SSRF/host-validation class uncovered, but nothing
broken is pinned).

### tests/test_materializer.py — TestConnectPageReplayIdempotency (lines 1173+)
**The dedup/missing-id fix chain IS pinned with realistic payloads against a real DB.**
The docstring records the superseding design: completed_works/payments are now
NON-resumable with a surrogate identity PK because the v2 export serializers omit a
per-row id (the tenant-765 production failure). The tests feed realistic id-less records
and verify full reload without duplication. This is exactly the regression pin the
mandate asked about — **but it skips without MANAGED_DATABASE_URL/DATABASE_URL, and CI
sets neither (§7), so the pin never executes in CI.**

### tests/test_materializer.py — orchestration classes (TestRunPipeline, TestResumableMaterialization)
Everything mocked: SchemaManager, MaterializationRun class with hand-set RunState attrs,
TenantMetadata, loaders, get_managed_db_connection, TransformationAsset. The
stale-cursor regression `test_completed_run_after_partial_invalidates_stale_cursor` pins
the *consumption* logic but injects prior_run via
`mock_run_cls.objects.filter.return_value.exclude.return_value.order_by.return_value.first.return_value`
— the real prior-run selection query semantics (filter/exclude correctness) are untested.
Resumable flags are passed explicitly, so the known two-contradictory-registries finding
is uncatchable here by construction.

### tests/test_ocs_data_loaders.py (269 LOC)
Mocks `loader._session.get` with plain MagicMock; happy-path only; zero
error-shape/retry/auth-failure tests. Confirms the known Connect-only-hardening
asymmetry at the test layer too: if OCS hardening is ever added, there is no harness to
receive it; if OCS error shapes drift, nothing fails.

---

## 6. Permissions adjudication (mandate question)

### apps/workspaces/permissions.py — dead-code claim STRENGTHENED **[NEW precision]**
The lens's open question was whether the DRF classes are "imported by views but never
effective." The answer is stronger: **grep across `apps/` and `tests/` finds zero
importers of IsWorkspaceMember/IsWorkspaceReadWrite/IsWorkspaceManager anywhere — only
the defining file matches.** They are entirely unreferenced dead code. All observed 403s
come from `aresolve_workspace` membership resolution and queryset filtering, which check
membership existence but (on most surfaces) not role.

### tests/test_workspace_permissions.py (106 LOC) **[NEW]**
- Defines `read_client`/`write_client` fixtures (lines 18–27) that NO test uses — dead
  fixtures advertising role coverage that does not exist.
- Every test is member-vs-non-member (200/403). Despite the suite's name, there are zero
  role-based assertions. The suite green is routinely cited as "permissions are tested";
  it tests membership only.
- Where role enforcement DOES exist, it is tested: transformations
  (test_transformation_api.py:152, 236, 311, 454 — read-role-forbidden on
  create/update/delete/trigger) and refresh (test_refresh_endpoint.py:61). The
  WorkspaceRole-unenforced gap is therefore confined to the knowledge/recipes/artifacts/
  chat/jobs surfaces, and the test suites for those surfaces (e.g. test_jobs_endpoints)
  use READ_WRITE fixtures exclusively, so the gap is structurally invisible to tests.

### tests/test_transformation_api.py (503 LOC)
Near-clean: real DB + APIClient throughout; the only mock is
`apps.transformations.services.executor.run_transformation_pipeline` in
`test_trigger_run` (line 399). Genuine pins: role enforcement, cross-tenant/foreign-
workspace 403s, system-scope immutability, container-reassignment immutability, lineage.
Uncatchable by design: everything inside the dbt executor (the known
dbt-superuser/no-SET-ROLE class) and the fact that the trigger runs the pipeline
synchronously in-request — the mock returns instantly, so request-blocking behavior under
a real multi-minute pipeline is invisible.

---

## 7. CI execution gap — the real-DB pins never run in CI **[NEW extension of known finding]**

The known finding covered the module-level MANAGED_DATABASE_URL skips
(test_view_schema_builder.py, test_ocs_materializer.py). The deep read extends it:
`.github/workflows/ci.yml` sets DATABASE_USER/PASSWORD/TEST_DATABASE_NAME/HOST/PORT but
**neither DATABASE_URL nor MANAGED_DATABASE_URL**, so even suites with a DATABASE_URL
*fallback* skip in CI. Net CI-dead set:

| Suite | What never runs in CI |
|---|---|
| test_view_schema_builder.py (788 LOC) | 63-byte truncation incident regressions |
| test_ocs_materializer.py | OCS materialization integration |
| TestConnectPageReplayIdempotency | the f26c1a0/2587158 dedup pin (§5) |
| TestWriteCases / TestWriteForms | real-DB writer semantics |
| TestExecuteAsyncIntegration | SET ROLE / search_path / statement_timeout enforcement |

Every "is the incident actually fixed" pin in the repo is in this set. They exist, they
are well-built, and they only run on developer laptops that happen to export the env
vars. A green CI badge certifies none of them.

---

## 8. tests/test_merge_users_service.py (452 LOC)

**Near-exemplary.** Real DB throughout; the only mock is
`patch("apps.users.services.merge._repoint_long_tail_fks", side_effect=RuntimeError)` to
prove transactional rollback. Covers canonical selection, field merges, SocialAccount/
EmailAddress repoint+dedupe, TenantMembership conflicts, TenantConnection OAuth conflict
merge, WorkspaceMembership role upgrade, introspection-driven long-tail FK repoint,
duplicate deletion, rollback, dry-run, M2M through-table skip.

Gap relative to known findings: **no TenantMetadata assertions anywhere in the suite**,
so the known "conflict path cascade-deletes the duplicate's TenantMetadata" mechanism is
exactly the one untested write. The email-verification merge gate lives in the command
layer (test_merge_duplicate_users_command.py, not read this round).

---

## Summary table

| Suite | Mock posture | Pins broken behavior? | Catches its incident class? |
|---|---|---|---|
| test_resume_thread_task | one agent seam, real DB | oauth_tokens dead plumbing | yes (cancel CAS, view-schema surfacing) |
| test_jobs_endpoints | status oracle + FutureApp seam mocked | no — RUNNING branch uncovered | no (reconciler false-failure invisible) |
| test_mcp_tenant_tools | model classes as MagicMocks | cancel "cancelled: True" surface | no (contract drift invisible; mock residue proves it) |
| test_sql_validator | pure unit (appropriate) | pg_catalog allowance pinned as contract | n/a |
| test_connect_data_loaders | requests_mock (right seam) | no | yes, payload-realistic |
| test_materializer orchestration | everything mocked | no | partially (consumption not selection) |
| TestConnectPageReplayIdempotency | real DB | no | yes — but CI-dead |
| test_ocs_data_loaders | MagicMock session | no | no error paths at all |
| test_workspace_permissions | real DB | implies coverage that isn't there | no role assertions |
| test_transformation_api | one executor mock | no | yes for authz; executor opaque |
| test_merge_users_service | one rollback mock | no | yes except TenantMetadata cascade |
