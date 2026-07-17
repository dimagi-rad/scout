# Git Historian — Architecture Review (Scout)

*Reviewer: git-historian (Phase 1, arch-review v2). Date: 2026-06-12. HEAD: 35e4230.*
*Mandate: regression archaeology, fix-chain analysis, fixed-where-it-bit sweeps.*
*Method: full git log (680 non-merge commits) read for fix clusters; every symptom seed
treated as the fixed instance of a bug class; sibling sites enumerated and read.*

---

## 1. Headline: the bug classes that were fixed where they bit, with live siblings

The history shows a consistent pattern: a production failure is fixed precisely at the
site that failed, with an excellent commit message describing the *class*, and the
sibling sites of that class are not audited. Five of the findings below are unfixed
siblings of bugs whose fix commits explicitly named the general mechanism.

### Finding H1 — Legacy refresh path still loads into the live schema, then destroys it (BROKEN-NOW)

**Status: BROKEN-NOW · Impact: data-loss (recoverable by re-materialization) · Confidence: verified-by-trace · Complexity: accidental**

This is v1 run A's S1, still live at HEAD. Two later fix commits touched this exact
function — `8104ce1` (SynchronousOnlyOperation in its credential resolution) and
`f0ed483` (TTL touch on its activation step) — without anyone re-checking its core
dataflow. The blue/green pretense is broken in the middle:

Chain (every hop quoted from HEAD):

1. **Entry**: Data Dictionary page refresh button →
   `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx:36` (`await refreshSchema()`) →
   `frontend/src/store/dictionarySlice.ts:197` (`api.post(\`/api/workspaces/${activeDomainId}/refresh/\`)`).
2. **View**: `apps/workspaces/api/views.py:362-365` — `new_schema = SchemaManager().create_refresh_schema(tenant)`
   then `refresh_tenant_schema.defer(schema_id=..., membership_id=...)`.
   `create_refresh_schema` (`apps/workspaces/services/schema_manager.py:176`) names the new
   schema `f"{sanitized}_r{uuid4().hex[:8]}"`, state PROVISIONING.
3. **Task**: `apps/workspaces/tasks.py:148` creates the physical `_r` schema; then
   `tasks.py:175` calls `run_pipeline(membership, credential, pipeline_config)` —
   **without passing the new schema**.
4. **Pipeline picks its own schema**: `mcp_server/services/materializer.py:183`
   `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`.
   `provision()` (`schema_manager.py:66-78`) returns the **existing ACTIVE schema**
   (filter `state__in=[ACTIVE, MATERIALIZING]`; the `_r` schema is PROVISIONING and has a
   different name, so it can never be selected). All data loads into the old live schema.
5. **Empty schema activated**: `tasks.py:182-185` — `new_schema.state = ACTIVE` (the `_r`
   schema, which received no data).
6. **Loaded schema destroyed**: `tasks.py:188-198` — every *other* ACTIVE schema for the
   tenant (i.e., the one the data was just loaded into) is flipped to TEARDOWN and
   `teardown_schema` is scheduled +30 minutes; `teardown_schema` drops it and flips its
   runs STALE (`tasks.py:633-639`).

Consequence: 30 minutes after a "successful" refresh, the workspace's only populated
schema is dropped; the ACTIVE schema is physically empty; the catalog (which reads
COMPLETED runs per schema) shows nothing. Additionally, per `d45b77e`'s teardown hook,
sibling multi-tenant workspaces' view schemas are flipped FAILED with no rebuild.

Reachability: live route (`/api/workspaces/<id>/refresh/`), wired to a UI button, gated
only by READ_WRITE/MANAGE role (`api/views.py:330`).

Historian note: `git log -S refresh_tenant_schema` shows the function has never been
rewritten since the `projects→workspaces` rename (62d329e); the refresh task predates
`run_pipeline`'s self-provisioning behavior — this is contract drift between
`refresh_tenant_schema` (believes it controls the target schema) and `run_pipeline`
(acquired its own `provision()` call), and the consumer was never migrated.

### Finding H2 — `build_view_schema` resurrects EXPIRED view-schema rows to ACTIVE without touching `last_accessed_at` — the exact Bug A of the 2026-06-10 incident, unfixed for WorkspaceViewSchema (LATENT)

**Status: LATENT · Impact: correctness · Confidence: verified-by-trace (missing touch); strong-inference (janitor-drop loop) · Complexity: accidental**

`f0ed483` fixed "provision() resurrect/activate paths flipped a schema to ACTIVE without
updating last_accessed_at" — for `TenantSchema` (in `provision()` and
`refresh_tenant_schema`). The same resurrect-without-touch lives on for
`WorkspaceViewSchema`:

- `apps/workspaces/services/schema_manager.py:280-287` — `build_view_schema` does
  `get_or_create(workspace=workspace, ...)`, which **reuses an existing row in any state,
  including EXPIRED**, then `vs.save(update_fields=["schema_name", "state"])`.
- `schema_manager.py:436-438` — on success: `vs.state = ACTIVE; vs.last_error = "";
  vs.save(update_fields=["state", "last_error"])`. `last_accessed_at` is never set
  anywhere in the build path.
- `teardown_view_schema_task` (`apps/workspaces/tasks.py:605-606`) leaves the EXPIRED row
  with its old `last_accessed_at`.
- The TTL janitor (`tasks.py:547-553`) expires any ACTIVE view schema with
  `last_accessed_at__lt=cutoff`.
- `touch_workspace_schemas` only touches view schemas in ACTIVE/MATERIALIZING, so no chat
  activity can refresh the timestamp while the row is EXPIRED or PROVISIONING.

Scenario (mirrors the incident's infinite re-materialization loop): multi-tenant
workspace idles past TTL → view schema EXPIRED with old timestamp → user returns, agent
re-materializes → `materialize_workspace` rebuilds the view schema → row goes ACTIVE with
a weeks-old `last_accessed_at` → if the user goes idle before the next `*/30` janitor
tick lands a chat-touch, the janitor tears the fresh view schema down again.

Fresh rows are safe (`last_accessed_at` NULL is excluded by `__lt`); the bite is
specifically the **reused EXPIRED row** — the same resurrect pattern named in
`f0ed483`'s own commit message.

### Finding H3 — The five surrogate-key Connect writers still commit per page while marked non-resumable: a failed load destroys the previous run's table (LATENT, regression introduced by 2587158)

**Status: LATENT · Impact: correctness (data availability) · Confidence: verified-by-trace · Complexity: accidental**

Fix-chain archaeology: `5421344` (#187) made Connect writers resumable (per-page commits
+ cursor watermark). `f26c1a0` added PKs to close the page-replay duplication gap.
`2587158` discovered the real Connect v2 serializers emit no `id`, switched the five
tables (`completed_works`, `payments`, `invoices`, `assessments`, `completed_modules`)
to `GENERATED ALWAYS AS IDENTITY` surrogate keys and flipped them to `resumable: false`
in `pipelines/connect_sync.yml:17-31` — **but left the per-page commits in the writers**.

Current state at HEAD:

- `_load_and_commit_source` documents the non-resumable contract: "the writer runs
  inside one transaction; this function calls `conn.commit()` once at the end"
  (`mcp_server/services/materializer.py:689-690`), with rollback on failure (line 718).
- But e.g. `_write_connect_completed_works` commits the `DROP TABLE` + `CREATE` up front
  (`materializer.py:1593-1621`, `conn.commit()` at 1621) and again after every page
  (line 1656), unconditionally — `cursor_callback` is None (yml non-resumable →
  `source_is_resumable=False` at `materializer.py:264`), so no watermark is ever
  persisted. Same pattern in the payments/invoices/assessments/completed_modules
  writers (`conn.commit()` at 1710/1741, 1790/1815, 1862/1887, 1932/1955).
- Contrast `_write_connect_users` (`materializer.py:1514-1570`): genuinely
  single-transaction; a failed load rolls back and the prior table survives.

Consequence: any Connect API failure mid-load (auth expiry, 5xx surviving retry) now
leaves the prior run's table **already dropped and committed**, replaced by an empty or
partial table. The source is recorded `failed`, so the catalog hides it
(`mcp_server/services/metadata.py:80`), i.e., previously-queryable data silently
disappears from the agent's view until a future full run succeeds. With no cursor and
`state != completed`, `_has_committed_cursor` is false, so a first-source failure marks
the run FAILED even though it destroyed data.

Residue: `_RESUMABLE_CONNECT_SOURCES` (`materializer.py:772-774`) still lists all six
sources — a second, now-contradictory resumability registry alongside the YAML flags.
Two registries for one fact is how this class recurs.

### Finding H4 — Bounded retry was added only to the Connect loader base; CommCare and OCS loaders have none (LATENT)

**Status: LATENT · Impact: cost-perf / correctness · Confidence: verified-by-trace · Complexity: accidental**

`59eb1fe` ("Add bounded retry + sentry-trace capture for Connect 5xx", 2026-05-27) gave
`mcp_server/loaders/connect_base.py` a urllib3 `Retry` (4 attempts, backoff 2.0,
status forcelist, `respect_retry_after_header`) — `connect_base.py:31-67,128`.

Siblings never got it:
- `mcp_server/loaders/commcare_base.py:64-68` — single `GET`, `raise_for_status()`.
- `mcp_server/loaders/ocs_base.py:45-52, 67-73` — same.

Both providers are also hard-gated non-resumable (`materializer.py:257` —
`is_resumable_provider = pipeline.provider == "commcare_connect"`), so one transient 5xx
or dropped connection mid-pagination fails the entire source and forces a full reload of
everything. For large CommCare form/case loads this is the dominant flake cost. OCS
per-session detail fetches (`ocs_sessions` loader) multiply the exposure: N+1 requests,
any one of which can kill the source.

### Finding H5 — The worker's dead-DB-connection fix was not applied to the MCP server, an identical long-lived Django-ORM process (LATENT)

**Status: LATENT · Impact: correctness (availability) · Confidence: strong-inference · Complexity: accidental**

The 2026-06-09 incident (22h of dead background processing) was diagnosed in
`config/procrastinate.py:36-60`: a long-lived process with no HTTP request cycle never
runs Django's connection-management hooks, so a connection that dies under it is "reused
— closed — forever". The fix is a custom task decorator (explicitly TEMPORARY, tracked
in #225, enforced for `apps.workspaces.tasks` by `tests/test_worker_db_resilience.py`).

The MCP server has the identical profile and got nothing:
- `mcp_server/__main__.py:13` — `django.setup()`; FastMCP process, no Django request
  cycle, no `request_finished` signal ever fires.
- It uses the Django ORM on every tool call: `mcp_server/context.py:66,125`
  (`await ts.atouch()` / `vs.atouch()`), `mcp_server/server.py` ThreadJob creation and
  MaterializationRun reads.
- `grep close_old_connections mcp_server/` → zero hits; `config/settings/base.py:120-122`
  sets no `CONN_MAX_AGE`/`CONN_HEALTH_CHECKS`.

After the next RDS maintenance window, every MCP tool call that touches the ORM should
fail with `OperationalError: the connection is closed` until the process is restarted —
the agent loses `query`/`list_tables`/`run_materialization` wholesale. The worker
incident is the existence proof of the mechanism; only the process differs.

### Finding H6 — Tenant schema names and readonly role names have no 63-byte identifier protection — siblings of the fixed view-name truncation (LATENT)

**Status: LATENT · Impact: security / correctness (cross-tenant collision) · Confidence: strong-inference · Complexity: accidental**

`2aaf4fb` fixed the 63-byte truncation-collision class for **view names only**, with a
commit message that names the general mechanism ("PostgreSQL silently truncates
identifiers to 63 bytes... the in-code collision check compared untruncated Python
strings"). Unprotected siblings at HEAD:

- `apps/workspaces/services/schema_manager.py:625-631` — `_sanitize_schema_name` has no
  length cap; `provision()` (line 66) uses it directly as the physical schema name. Two
  tenants whose sanitized `external_id`s share a 63-byte prefix get **the same physical
  schema** while their `TenantSchema` rows (unique on the untruncated 255-char string)
  stay distinct — cross-tenant data overwrite, invisible to the Django layer. Exposure
  is provider-dependent: Connect ids are ints and OCS ids are short; long CommCare
  domain names are the realistic vector.
- `schema_manager.py:33-35` — `readonly_role_name = f"{schema_name}_ro"`: a 61+ char
  schema name yields a role that truncates into other roles' names (roles are
  cluster-global).
- `schema_manager.py:176` — `create_refresh_schema` appends `_r{8hex}` (+10 chars): for a
  sanitized name ≥54 chars the random suffix is partially or wholly truncated away,
  eroding its uniqueness guarantee.

The input-validation family already recurred twice (connect-name truncation in v1;
view-name truncation in the incident). This is the third member, pre-bite.

### Finding H7 — RecipesPage does not refetch on workspace switch — asymmetric sibling of the threadId cross-workspace fix (LATENT)

**Status: LATENT · Impact: correctness · Confidence: strong-inference · Complexity: accidental**

`00c423d` fixed thread-carrying across workspaces by resetting exactly one piece of
state (`threadId`) in `setActiveDomain` (`frontend/src/store/domainSlice.ts:53-70`). No
general "reset per-workspace state on switch" mechanism was added. The slices all key
their fetches off `activeDomainId` at call time but cache results globally:

- `frontend/src/pages/RecipesPage/RecipesPage.tsx:45-47` —
  `useEffect(() => { fetchRecipes() }, [fetchRecipes])` — zustand actions are stable, so
  this never re-runs on workspace change while mounted.
- Contrast `frontend/src/pages/KnowledgePage/KnowledgePage.tsx:56` — correctly depends on
  `[activeDomainId, ...]`.

If the active workspace changes while /recipes is mounted, the list (and any
detail/runs state in `recipeSlice`) remains the previous workspace's; clicking through
issues requests against the new workspace with old ids → 404s. Severity depends on
whether the current switcher UX can change workspace without unmounting the page (the
June top-bar redesign navigates to the new workspace's settings page in at least one
path, which would mask this); I did not verify every switch path — hence
strong-inference, not verified.

### Finding H8 — ThreadJob create-after-defer race: a 19-commit fix chain converged on a hedge, not a fix (DEBT)

**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The resume/ThreadJob cluster (19 commits, 2026-05-20→28: `0833e7e`, `4f0def1`,
`36ab61f`, `2fc7e45`, `bc82ab8`, `9818e4c`, `38473a7`, `a18e253`, ...) repeatedly patched
one unowned mechanism: MCP commits the ThreadJob row *after* `defer_async` returns
(`mcp_server/server.py` run_materialization), so the worker can finish before the row
exists. The terminal state of the chain is a bounded-backoff hedge:
`apps/workspaces/tasks.py:362-396` (`_defer_resume_for_job`, retries totalling ~3.75s,
then "janitor will catch up"), with the TODO at `tasks.py:373-377` admitting the clean
fix (MCP pre-creates a placeholder ThreadJob; needs `procrastinate_job_id` nullable —
"a migration we are skipping for this PR").

Regression archaeology from inside the chain: `2fc7e45` (May 21, "tighten resume CAS to
prevent duplicate ainvoke") dropped CANCELLED from the CAS filter and broke the
cancellation follow-up path; `bc82ab8` restored it the same day. A state machine whose
legal transitions live only in a CAS filter argument will keep doing this.

### Finding H9 — The janitor itself was silently dead for ~a day via a bare `except` defaulting to "status unknown" (fixed in 28b6647) — the silent-fallback class on *rescue paths* has now bitten twice (DEBT)

**Status: DEBT · Impact: correctness · Confidence: verified-by-trace (historical) · Complexity: accidental**

Archaeology, because the mechanism is instructive: `e91ff9b` (June 9 incident response)
introduced `_procrastinate_job_status` reading via a module-level import of
procrastinate's `current_app`. That name binds to the `FutureApp` blueprint at import
time and is later rebound *in procrastinate's module*, not in the importer — so in the
production worker every status call raised `AttributeError`, which a bare `except`
swallowed as "status unknown", and the janitor skipped every stale ThreadJob forever
(five zombies from the 06-09 incident stayed stuck). Fixed next day by `28b6647`
(read `procrastinate_jobs` via the contrib ORM model).

This is the second time the rescuer was dead when needed (2026-05-30: zombie `doing`
jobs the janitor could not rescue). Pattern for the error-handling lens: on
reconciliation/janitor paths, an exception swallowed into a benign default converts
"rescue system broken" into silence. I did not exhaustively sweep remaining bare
excepts on rescue paths — flagged for the gap loop.

### Finding H10 — Share surface drift: creation UI removed, tokens and public endpoints remain live (DEBT)

**Status: DEBT · Impact: security (low) · Confidence: verified-by-trace (wiring) · Complexity: accidental**

`9783eb2` (2026-06-04) removed the share-creation UI only. At HEAD,
`config/urls.py:102` (`api/recipes/runs/shared/<share_token>/`) and `config/urls.py:107`
(`api/chat/threads/shared/<share_token>/`) remain unauthenticated routes; share fields
and public pages remain. Any token minted before 06-04 still grants access and there is
no remaining UI to revoke or even enumerate shares. (Known seed; confirmed still true —
the historian's addition is that the removal commit's scope was UI-only by design, with
no follow-up issue visible in the log.)

---

## 2. Smaller observations (residue / drift)

- **Dual resumability registries** (see H3): `pipelines/connect_sync.yml` flags vs
  `_RESUMABLE_CONNECT_SOURCES` (`materializer.py:772`) vs the provider gate
  (`materializer.py:257`). Also `pipeline_registry.py:25` defaults `resumable=True`
  for every source of every provider, though only Connect implements it — a misleading
  default for the next provider added.
- **`row_count` → `materialized_row_count` rename (eba96d1) completed cleanly.** The
  remaining `row_count` keys (`mcp_server/services/query.py:62,92,141`,
  `apps/artifacts/views.py:838`, frontend ToolOutput/ArtifactPanel) are the *live query
  result* semantic, not the catalog field; frontend consumes `materialized_row_count`
  (`ToolOutput.tsx:254,286`). No stale consumers found.
- **Prompt ↔ SQL-validator alignment (93504d5) is holding**: both sides are SELECT-only
  (validator `sql_validator.py:222-243`; prompt `base_system.py:163`); no
  post-alignment commit touched one file without the other in a way that re-drifted the
  contract (checked `git log` on both files).
- **Model/param drift**: clean. `378121e` removed temperature; the only reference left
  is an explanatory comment (`apps/agents/graph/base.py:515`); model configurable via
  `DEFAULT_LLM_MODEL` (`config/settings/base.py:281`).
- **Ownership filters added in the May-21 security fixes are present at the sibling I
  checked**: `apps/workspaces/api/jobs_views.py:104-109` filters
  `thread__workspace=workspace, thread__user=user`. (Full authz sweep belongs to the
  authz lens.)

## 3. Fix-chain map (for the synthesis phase)

| Chain | Commits | Terminal state | Residual risk |
|---|---|---|---|
| ThreadJob/resume | 19, 05-20→28 | hedge + janitor (H8) | acknowledged race; CAS-encoded state machine |
| Materialization atomicity/catalog | de4b1ac, 0469fc9, 5421344, eba96d1, 4da65d3, f26c1a0, 2587158 | surrogate keys, non-resumable | H3 (per-page commits kept) |
| View-schema lifecycle | 8fb212e, e451c7a, 2aaf4fb, ef42b4e, d45b77e | post-incident hardening | H2 (TTL touch), H6 (identifier siblings) |
| Worker connection hygiene | e91ff9b, b7b61af, ab4b426, fe93603, 28b6647 | temporary decorator (#225) | H5 (MCP server uncovered); decorator removal owes an upstream-watch |
| Connect loader integrity | a229993, 8774864, 59eb1fe, f26c1a0, 2587158 | retry + surrogate keys | H4 (no retry for CommCare/OCS) |
| Panic loop / prompt drift | f738ab9, 93504d5 | aligned | watch class on next validator change |

## 4. What's fine (verified healthy)

- `refresh_tenant_schema`'s *peripheral* fixes are correct (async credential resolver,
  TTL touch) — the core dataflow is the problem (H1).
- Connect page-replay duplication is genuinely closed: non-resumable + DROP/CREATE per
  run means no replay path exists for the five id-less tables; `raw_visits` uses
  `ON CONFLICT (visit_id) DO UPDATE` (`materializer.py:1271`).
- CommCare `meta.next` handling is centralized in `_resolve_next_url`
  (`commcare_base.py`, all three URL shapes) with regression tests (`336a29f`).
- The TTL janitor's STALE-flip-deferred-to-teardown design (`tasks.py:524-530` comment)
  matches the implementation (`tasks.py:633-639`).
- The worker connection-hygiene decorator is documented, tracked (#225), and enforced by
  a test that every task in `apps.workspaces.tasks` registers through it.
- `materialized_row_count` rename, prompt/validator alignment, model-param cleanup — no
  residue (details §2).

## 5. Coverage log

**Deep-read** (line-level, in service of specific traces):
`mcp_server/services/materializer.py` (orchestration 96–400, load/commit 632–830, writers 1390–1960),
`apps/workspaces/tasks.py` (refresh 100–200, resume-defer 355–420, janitors/teardown 518–645),
`apps/workspaces/services/schema_manager.py` (1–60, provision 60–130, view-prefix/build 214–360, 425–445, sanitizer 625–631),
`config/procrastinate.py` (full), `mcp_server/loaders/ocs_base.py` (full),
`mcp_server/loaders/connect_base.py` + `commcare_base.py` (retry/pagination regions),
`pipelines/connect_sync.yml` (full), `frontend/src/store/domainSlice.ts` (full),
`apps/workspaces/api/views.py` (refresh view 330–375), `mcp_server/__main__.py` (full),
fix-commit diffs in full: f26c1a0, 2587158, 2aaf4fb, f0ed483, ef42b4e, d45b77e, 28b6647, 00c423d, bc82ab8.

**Skimmed** (grep-level or partial): `mcp_server/server.py`, `mcp_server/services/metadata.py`,
`mcp_server/services/sql_validator.py`, `apps/agents/prompts/base_system.py`,
`mcp_server/context.py`, `apps/workspaces/api/jobs_views.py`, `config/urls.py`,
`config/settings/base.py`, `frontend/src/store/*` slices, `RecipesPage.tsx`/`KnowledgePage.tsx`/`DataDictionaryPage.tsx` (effect wiring only), `mcp_server/pipeline_registry.py`.

**Not examined** (honest gaps for the gap loop):
the 2026-02/03 fix bursts in detail (transformations authz chain 2d791ab/a19ce46 sibling sweep not done);
`apps/users/services/merge.py` and the OAuth/allauth signal chain; `apps/agents/graph/base.py` internals
(panic-loop breaker, tool-schema rewriting); `apps/chat/stream.py`/checkpointer; `apps/artifacts/*`
(incl. export, sandbox, widget.js); `apps/recipes/services/runner.py`; `apps/transformations/*` and dbt
runner; OCS/CommCare individual loader files beyond the base classes; `purge_synced_data` and other
management commands; `mcp_server/envelope.py`/`auth.py`; deploy configs (`config/deploy*.yml`, .kamal);
tests-as-architecture; frontend beyond the files named above; `workspaces/permissions.py` dead-code claim
(took the seed at face value, did not re-verify); remaining bare-except sweep on rescue paths (H9 pattern).
