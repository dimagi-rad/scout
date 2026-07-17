# Vertical review: provider data loaders + materializer writer functions

*Reviewer: extra-vertical-provider-data-loaders. Scope: `mcp_server/loaders/` (19 files),
`mcp_server/services/materializer.py` per-table writers and orchestration, the three
external API contracts (CommCare, Connect, OCS), and writer/loader contract drift
against `pipelines/*.yml`. Report only; no code changed.*

## Summary

The Connect loader stack is the most mature of the three providers — it absorbed two
production bugs (page-replay duplication `f26c1a0`, missing-id crash `2587158`) and has
retries, structured errors, and regression tests. The sibling loaders did **not** receive
the equivalent hardening: CommCare and OCS have no retry policy, the CommCare cases
loader missed the relative-`next`-URL fix its siblings got, and OCS silently tolerates a
missing `results` key where Connect raises. The deepest problems are not in the loaders
but in the materializer's transactionality contract (writers commit per page while the
orchestrator and docstrings believe non-resumable sources are atomic) and in the
**still-live legacy refresh path**, which loads fresh data into the old schema and then
destroys it — v1 run A's S1, confirmed unchanged at HEAD `35e4230`.

Important context on the two "fixed" Connect bugs: they interlock. `f26c1a0` fixed
page-replay duplication by adding natural-key PKs + `ON CONFLICT` to 5 tables. Then
`2587158` discovered the Connect serializers emit no per-row `id`, **reverted** to
surrogate identity PKs with no `ON CONFLICT`, and disabled resume for those 5 sources in
`connect_sync.yml`. The duplication protection therefore no longer exists in code — the
only guard is a YAML flag plus upstream serializer behavior, while the writers still
carry full (now-dead) resume plumbing that claims otherwise.

---

## Findings

### F1 — Legacy refresh path loads fresh data into the old schema, then destroys it (v1 S1, still live)

- **Status**: BROKEN-NOW · **Impact**: data-loss (materialized warehouse + service outage until re-materialization) · **Confidence**: verified-by-trace · **Complexity**: accidental
- **Reachable via**: Data Dictionary page refresh button (live UI).

Chain (every hop verified at HEAD):

1. `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx:33-40` — `handleRefresh` → `refreshSchema()`.
2. `frontend/src/store/dictionarySlice.ts:197` — `api.post(\`/api/workspaces/${activeDomainId}/refresh/\`)`.
3. `apps/workspaces/api/urls.py:22` — `path("refresh/", RefreshSchemaView...)`.
4. `apps/workspaces/api/views.py:362` — `new_schema = SchemaManager().create_refresh_schema(tenant)` → `schema_manager.py:176` names it `{base}_r{uuid8}` (a **new, suffixed** schema row).
5. `apps/workspaces/api/views.py:365` — defers `refresh_tenant_schema(schema_id, membership_id)`.
6. `apps/workspaces/tasks.py:149-150` — creates the physical `_r` schema; `tasks.py:173` — `await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)` — **`new_schema` is never passed to the pipeline**.
7. `mcp_server/services/materializer.py:183` — `run_pipeline` self-provisions: `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`.
8. `apps/workspaces/services/schema_manager.py:66-78` — `provision` resolves by the **base** name `_sanitize_schema_name(external_id)` and returns the existing ACTIVE schema (the OLD one). All writers DROP/CREATE/INSERT inside that old schema (e.g. `materializer.py:1419`).
9. Back in the task: `tasks.py` Step 3 marks the untouched, **empty** `_r` schema ACTIVE; Step 4 flips every other ACTIVE schema for the tenant — i.e. the base schema that just received the freshly loaded data — to TEARDOWN and schedules `teardown_schema` in 30 minutes.

Consequence: tenant context (`mcp_server/context.py:58` selects ACTIVE/MATERIALIZING)
now resolves to the empty `_r` schema — agent sees no tables immediately — and the
schema containing the fresh load is physically dropped 30 minutes later. Both branches
lose: if no old ACTIVE schema existed, `provision` creates and fills the base-named
schema, which Step 4 then tears down anyway.

This is the same finding v1 run A flagged; it survived the entire #227–#232 incident-fix
wave. The root cause is a contract mismatch: `run_pipeline` owns provisioning, while
`refresh_tenant_schema` believes it owns the target schema.

### F2 — Writers violate the orchestrator's transactionality contract: "non-resumable" Connect sources commit per page

- **Status**: LATENT · **Impact**: correctness · **Confidence**: verified-by-trace · **Complexity**: accidental

The module docstring (`materializer.py:4-7`) and `MaterializationCancelled` docstring
(`materializer.py:88-93`: "the open psycopg transaction rolls back (no partial data is
written)") and `_load_and_commit_source` (`materializer.py:688-697`: "Non-resumable
path...the writer runs inside one transaction; this function calls conn.commit() once at
the end") all assert single-transaction semantics for non-resumable sources.

But all seven Connect writers call `conn.commit()` unconditionally — after DDL and after
**every page** — regardless of the resumable flag: `materializer.py:1450,1490` (visits),
`1621,1656` (completed_works), `1710,1741` (payments), `1790,1815` (invoices),
`1862,1887` (assessments), `1932,1955` (completed_modules). Since `2587158` marked five
of these `resumable: false` in `pipelines/connect_sync.yml`, the orchestrator routes
them down the "atomic" path (`run_pipeline` passes `resumable=False`,
`start_cursor=None`, `cursor_callback=None`) while the writers keep committing
page-by-page (`_load_connect_source` still dispatches them through the cursor-aware
signature because `_RESUMABLE_CONNECT_SOURCES` at `materializer.py:772-774` still lists
all six).

Consequences when a Connect source fails or is cancelled mid-load:
- A durably committed **partial table** remains in the tenant schema; the rollback in
  `_load_and_commit_source:716-724` rolls back only the last uncommitted page.
- The run record claims otherwise: the cancel handler comment (`materializer.py:18-19`,
  `311-314`) says "this in-flight source rolled back", and the failed/cancelled source
  entry records `rows: 0`.
- The catalog hides it (`metadata.py:80` filters `state != "completed"`), **but the
  `query` tool does not**: `sql_validator.py` enforces a schema allowlist, not table
  state, and `context.py:58` serves ACTIVE schemas. An agent that knows the table name
  (from an earlier turn, a Learning, or a saved artifact query) reads partial data with
  no signal.

The next clean run DROPs and rebuilds, so the window is bounded — but during it, the
system state contradicts every piece of its own documentation and run metadata.

### F3 — Mid-rematerialization reads: Connect serves silently-partial tables; CommCare/OCS block queries instead

- **Status**: LATENT (window opens on every re-materialization of an active tenant) · **Impact**: correctness · **Confidence**: verified-by-trace · **Complexity**: mixed (a snapshot-swap design would avoid it; current in-place rebuild makes it inherent)

During re-materialization the schema stays ACTIVE the whole time (`provision` returns
the existing ACTIVE row and only touches it, `schema_manager.py:73-78`; state is
re-written at completion, `materializer.py:485-486`), and `context.py:58` happily serves
it.

- Connect writers commit `DROP TABLE` + `CREATE TABLE` immediately
  (`materializer.py:1418-1450`), then grow the table page-committed-page. A concurrent
  agent `query` between commits returns an empty or partially-loaded `raw_visits` with
  no error, warning, or flag.
- CommCare and OCS writers hold the `DROP TABLE` inside one transaction
  (`_load_and_commit_source` commits once at the end), so concurrent reads block on the
  ACCESS EXCLUSIVE lock until `statement_timeout` (30s, `context.py:159`) and fail with
  a timeout for the duration of a potentially hours-long load.

Two opposite failure modes for the same situation, neither communicated to the agent or
user. The catalog-level guard (`metadata.py:80`) protects `list_tables` only.

### F4 — Page-replay duplication protection was removed; resume safety now rests on a YAML flag plus upstream serializer behavior

- **Status**: LATENT · **Impact**: correctness (silent row duplication if re-armed) · **Confidence**: verified-by-trace (code state) / strong-inference (re-arming scenario) · **Complexity**: accidental

History: `f26c1a0` fixed the cross-DB commit/watermark race (page committed to tenant
DB, then watermark persisted to platform DB — a crash between the two replays the page
on resume) by giving the five id-less Connect tables natural-key PKs + `ON CONFLICT (id)
DO NOTHING`. `2587158` then found that the Connect v2 serializers emit **no per-row
`id`** for those resources (every insert had been crashing with NotNullViolation),
switched them to `GENERATED ALWAYS AS IDENTITY` surrogate keys, **dropped the ON
CONFLICT**, and set `resumable: false` in `connect_sync.yml`.

Current state:
- The only thing preventing the original duplication bug is `resumable: false` in
  `pipelines/connect_sync.yml` + the absence of upstream `id` fields. If Connect ever
  adds `id` to those serializers and anyone flips the flag back (the writers'
  docstrings invite this — see below), the crash-window duplication returns with no
  code-level defense and no test that would catch it (the replay tests now use id-less
  fixtures per `2587158`).
- Dead/misleading residue: `_RESUMABLE_CONNECT_SOURCES` (`materializer.py:772-774`)
  still lists all five; its comment says only `users` is excluded "intentionally —
  mutable rows", contradicting the yml which excludes five more for a different reason.
  All five writers retain full resume plumbing (`start_cursor`, `cursor_callback`,
  `CREATE TABLE IF NOT EXISTS`, per-page `_max_id(page, "id")` that can never find an
  id) and docstrings beginning "Resumable: when ``start_cursor`` is set..." describing
  behavior the configuration forbids.
- The visits crash-window is genuinely safe: `ON CONFLICT (visit_id) DO UPDATE`
  (`materializer.py:1271-1274`) absorbs the replayed page. The commit-then-checkpoint
  ordering (`materializer.py:1487-1492`) is correct *only because* of that upsert.

### F5 — `materialized_row_count` is wrong after a resumed visits run

- **Status**: LATENT (manifests whenever a visits resume occurs) · **Impact**: correctness · **Confidence**: verified-by-trace · **Complexity**: accidental

On resume, `_write_connect_visits` starts `total = 0` (`materializer.py:1453`) and
counts only this run's pages. The completion entry stores `rows: <writer return>`
(`materializer.py:375-381`), and `pipeline_list_tables` surfaces that as
`materialized_row_count` (`metadata.py:90`). A table with 50k rows from the failed run
plus 10k from the resume reports 10k. `row_count_verified: False` softens this, but the
number is wrong by construction, and `expire`/status summaries (`tasks.py` aggregation,
"N rows loaded successfully") repeat it.

### F6 — Visits resume violates the same mutability rule that made `users` non-resumable

- **Status**: DEBT · **Impact**: correctness (stale review statuses presented as fresh) · **Confidence**: strong-inference · **Complexity**: mixed

`users` is non-resumable because "rows are mutable... a partial resume could miss
in-place updates behind the cursor" (`connect_sync.yml`, `materializer.py:770-771`). But
visit rows mutate too — `status`, `review_status`, `status_modified_date` are exactly
the fields `_CONNECT_VISITS_INSERT`'s `ON CONFLICT ... DO UPDATE`
(`materializer.py:1271-1274`) exists to refresh. Keyset resume (`last_id` strictly
greater) never re-fetches rows behind the watermark, so a resumed run completes with a
temporal mix: pre-watermark rows carry the failed run's statuses, post-watermark rows
are current — all stamped with the new run's `materialized_at`. Accepted trade-off in
issue #187 for crash recovery, but the inconsistency with the users rationale is
undocumented and review-status analytics (a core Connect use case) silently degrade.

### F7 — CommCare cases loader missed the relative-`next`-URL sibling fix

- **Status**: LATENT · **Impact**: correctness (full source failure) · **Confidence**: verified-by-trace (code gap) / hypothesis (API trigger) · **Complexity**: accidental

`8774864` (a sentry-bot fix, i.e. it bit in production) added
`_resolve_next_url` to handle CommCare returning path-relative or query-string-only
`next` URLs — and applied it to `commcare_forms.py:70` and `commcare_metadata.py:55`
only. `commcare_cases.py:68` still does `url = data.get("next")` raw. If the Case API
v2 ever returns a non-absolute `next` (the observed behavior of sibling endpoints on the
same server), `requests` raises `MissingSchema` and the cases source fails entirely.
Classic fixed-where-it-bit gap; one-line sibling fix.

### F8 — No retry policy for CommCare or OCS loaders (Connect-only hardening)

- **Status**: DEBT · **Impact**: cost-perf (full-source restarts) · **Confidence**: verified-by-trace · **Complexity**: accidental

`ConnectBaseLoader` mounts a urllib3 `Retry` (3 retries, backoff, 5xx/429/408,
`connect_base.py:61-69,128-130`) — added after production 5xx failures. Neither
`CommCareBaseLoader` (`commcare_base.py:61-69`) nor `OCSBaseLoader`
(`ocs_base.py:45-52,67-73`) has any retry: one transient 502 anywhere in a multi-hour
paginated walk kills the whole source, and since neither provider is resumable, the next
run reloads everything. Worst case is `OCSMessageLoader` (`ocs_messages.py:46-52`): one
GET **per session** (N+1, acknowledged in the docstring) with zero retries — failure
probability compounds with session count, and a single deleted-session 404 mid-walk also
aborts the entire messages source.

### F9 — OCS pagination silently treats a missing `results` key as end-of-data

- **Status**: LATENT · **Impact**: correctness (silent empty/short tables marked "completed") · **Confidence**: verified-by-trace · **Complexity**: accidental

`ocs_base.py:75`: `page = payload.get("results", [])`. A 200 response without
`results` (error envelope, contract change, proxy interference) yields an empty page
and, if `next` is also absent, terminates the loop normally — the source completes with
0 rows, gets `state: "completed"` and enters the catalog as an empty table. The Connect
sibling raises `ConnectExportError` on exactly this condition
(`connect_base.py:222-223`). Same silent-degradation class the project has been burned
by (view-schema failures swallowed, 2026-06-10 incident item d).

### F10 — Inconsistent missing-primary-key semantics across providers: silent row collapse vs crash

- **Status**: LATENT · **Impact**: correctness · **Confidence**: strong-inference (mechanism verified; missing-id frequency unknown) · **Complexity**: accidental

- OCS sessions/participants and CommCare cases default a missing id to `""`
  (`ocs_sessions.py:50`, `ocs_participants.py:68`, `commcare_cases.py:78`) and their
  INSERTs upsert on that PK (`_OCS_SESSIONS_INSERT`, `_OCS_PARTICIPANTS_INSERT`,
  `_CASES_INSERT`): multiple id-less records silently collapse into one `""`-keyed row.
- Connect visits passes `None` (`connect_visits.py:44`) into a `BIGINT PRIMARY KEY` →
  NotNullViolation → whole source fails (the `2587158` failure mode).
- OCS messages skips id-less sessions entirely (`ocs_messages.py:41-42`) — silent
  undercount.

Three behaviors for one condition; none is logged or surfaced as a data-quality signal.

### F11 — `_sanitize_schema_name` is unbounded and lossy: the 63-byte identifier class, schema edition

- **Status**: LATENT · **Impact**: security (cross-tenant schema collision) · **Confidence**: hypothesis (mechanism verified; realistic colliding IDs unconfirmed) · **Complexity**: accidental

`schema_manager.py:625-631` lowercases, maps `-`→`_`, strips other characters, with no
length limit; `TenantSchema.schema_name` is `max_length=255` (`models.py:33`) while
PostgreSQL silently truncates identifiers at 63 bytes. Two distinct tenant external_ids
that sanitize identically (`my-domain` vs `my_domain`, `a.b` vs `ab`) or share a 63-byte
prefix would map to **one physical schema** — `CREATE SCHEMA IF NOT EXISTS` and
`provision`'s name-based lookup (`schema_manager.py:66-71`) would happily co-locate two
tenants, and the writers' `DROP TABLE` would destroy each other's data. Connect IDs
(ints) and OCS IDs (UUIDs) are safe; CommCare domains with hyphens/length are the
exposure. This is the same input-validation family as the 2026-06-10 63-byte view-name
incident (seed 12) — the view-name site was fixed; this sibling site was not audited.

### F12 — Loaders follow server-supplied `next` URLs anywhere, with credentials pinned to the session

- **Status**: LATENT · **Impact**: security (credential exfiltration requires a compromised/misbehaving provider) · **Confidence**: hypothesis · **Complexity**: accidental

`connect_base.py:234` and `ocs_base.py:84` set the next request URL directly from the
response body; auth lives in session headers (`Authorization` / `X-api-key`), which
`requests` sends to whatever host/scheme the `next` URL names (header-stripping applies
only to redirects, not to body-supplied URLs). Production Connect already returns
scheme-downgraded `http://` next URLs (acknowledged at `connect_base.py:187-194`). No
host/scheme validation is applied. Related unchecked TODO.md item: "loader network
egress restriction".

### F13 — CommCare offset pagination under live writes can silently skip records

- **Status**: DEBT · **Impact**: correctness · **Confidence**: hypothesis · **Complexity**: essential (API limitation) — mitigation accidental-missing

`commcare_forms.py:44-71` walks tastypie `limit/offset` pages with no explicit ordering
param. Submissions arriving mid-walk shift offsets: duplicates are absorbed by
`ON CONFLICT (form_id)` (though they inflate the `total += len(page)` row count,
`materializer.py:1226-1227`), but **skips are silent and permanent** until the next full
reload. The Connect keyset (`last_id`) design does not have this problem; CommCare was
never given an equivalent (e.g. `server_modified_on` ordering + watermark).

### F14 — Documentation/configuration drift cluster in the writer layer

- **Status**: COSMETIC (but it actively misleads maintainers) · **Impact**: velocity · **Confidence**: verified-by-trace

- `materializer.py:1910` docstring: "``duration`` becomes INTEGER (seconds)" — column is
  `duration TEXT` (`materializer.py:1927`), value defaulted to `""`.
- Module docstring `materializer.py:4-7` + `materializer.py:88-93` claim atomic
  non-resumable sources — false for all Connect writers (F2).
- Five writers' "Resumable:" docstrings describe a path their yml config forbids (F4).
- `_RESUMABLE_CONNECT_SOURCES` comment (`materializer.py:770-771`) says only `users` is
  excluded; the yml excludes six of seven.
- `SourceConfig.resumable` comment (`pipeline_registry.py:22-24`) still cites
  "Append-mostly Connect resources (visits, completed_works, etc.) keep the default
  True" — completed_works has been `false` since `2587158`.
- `schema_manager.py:174` comment still says "Celery task" (Procrastinate migration
  residue).

### F15 — Completion-time `last_accessed_at` write can regress a fresher touch

- **Status**: COSMETIC · **Impact**: correctness (TTL bookkeeping, bounded) · **Confidence**: verified-by-trace

`materializer.py:485-486` saves the in-memory `tenant_schema` with
`update_fields=["state", "last_accessed_at"]`; the instance's `last_accessed_at` is the
provision-time value (`touch()` at `schema_manager.py:77`). A multi-hour load during
which the MCP context touched the schema ends with the timestamp rolled back to
provision time. Practically harmless against a long TTL, but it is a stale-instance
write into the exact column triangle behind the 2026-06-10 TTL incident.

---

## Contract drift matrix (loader ↔ writer ↔ pipeline yml)

| Source | Loader resume support | yml `resumable` | Writer behavior | Drift |
|---|---|---|---|---|
| connect visits | `start_last_id` ✓ | true (default) | per-page commit + upsert + cursor | consistent; F5/F6 caveats |
| connect users | no param | false | DROP/CREATE, page-commit-free? No — single txn writer | consistent |
| connect payments/invoices/assessments/completed_works/completed_modules | `start_last_id` ✓ (dead) | false | per-page commit, dead cursor plumbing, no ON CONFLICT | F2, F4, F14 |
| commcare cases/forms | none | n/a (provider gate) | single txn, upsert PK | F3 (lock window), F7, F13 |
| ocs experiments/sessions/messages/participants | none | n/a | single txn, upsert PK | F3, F8, F9, F10 |

## What's actually fine

- **`ConnectBaseLoader`** is genuinely well-engineered: bounded retry with backoff and
  Retry-After respect, auth-error separation, structured `ConnectExportError` carrying
  status/attempts/sentry-trace/last_id for operator correlation, versioned Accept header
  scoped per-call, and a documented http→https redirect pin with a regression test.
- **The visits resume watermark design** (commit-then-checkpoint, upsert-absorbing
  replay, `_load_prior_resume_cursors` refusing stale cursors after an intervening
  COMPLETED run and refusing non-PARTIAL/FAILED runs) is correct for the one source
  where it is enabled.
- **Catalog reconciliation** (`pipeline_list_tables`) correctly hides non-completed
  sources and phantom tables against `information_schema` (#185/#187 fixes hold), and
  fails closed (empty catalog) on enumeration errors.
- **CAS-guarded state transitions** in `run_pipeline` (DISCOVERING→LOADING,
  LOADING→TRANSFORMING, TRANSFORMING→COMPLETED) preserve concurrent CANCELLED; the
  pre-loop failure handler guarantees a terminal run state; the step-count drift guard
  is a nice touch.
- **`_json_or_none`** correctly preserves SQL NULL vs JSONB `null`.
- **Progress honesty** is consistently engineered (real denominators, the
  sessions-denominated OCS messages unit, indeterminate-on-resume) per project norms.
- **Loader unit tests** cover happy paths and several real regressions well (page
  replay idempotency, Accept headers, auth errors, nested-object extraction, redirect
  following).
- `pipeline_registry.py` is simple and safe (per-file error isolation, sensible
  defaults, single `physical_table_name` convention the writers all honor).

## Coverage log

**Deep (line-by-line):** all 19 files in `mcp_server/loaders/`;
`mcp_server/services/materializer.py` (all 1,973 lines, all 13 writer functions);
`pipelines/commcare_sync.yml`, `connect_sync.yml`, `ocs_sync.yml`;
`mcp_server/pipeline_registry.py`; `mcp_server/services/metadata.py`;
`apps/workspaces/services/schema_manager.py` (provision, create_refresh_schema,
`_sanitize_schema_name` regions); `apps/workspaces/tasks.py` lines ~90–300 (refresh +
materialize_workspace call paths); git commits `f26c1a0`, `2587158`, `8774864`.

**Skimmed:** `mcp_server/context.py` (state-filter and search_path lines only);
`mcp_server/services/sql_validator.py` (grep-level: schema allowlist, no table-state
gating); `tests/test_connect_data_loaders.py` and `tests/test_ocs_data_loaders.py`
(test names/structure only); `frontend/src/store/dictionarySlice.ts` and
`DataDictionaryPage.tsx` (refresh wiring only); `apps/workspaces/api/views.py`
(refresh endpoint region only); `apps/workspaces/models.py` (field lengths only).

**Not examined:** `mcp_server/server.py` (tool definitions, fire-and-ack), 
`mcp_server/envelope.py`, `mcp_server/auth.py`, `mcp_server/services/query.py`
internals, `mcp_server/services/dbt_runner.py`; the transform phase consumers
(`apps/transformations/` executor/staging/lineage); test bodies of
`tests/test_materializer.py`, `test_ocs_materializer.py`, `test_commcare_*`;
`apps/workspaces/tasks.py` janitors/resume/teardown sections (lines 300+);
view-schema build internals (`schema_manager` view functions); the actual upstream
provider API contracts (commcare-connect, open-chat-studio, CommCare HQ source) — all
claims about upstream response shapes are inferred from this repo's code, comments, and
fix-commit messages, not verified against provider source; OCS `/api/participants`
endpoint behavior; whether CommCare Case API v2 can return relative `next` URLs (F7
trigger condition).
