# Lens report: Input validation & external-data boundaries

*Reviewer: lens — input-validation / external-data boundary. Scope: identifier
length/shape assumptions, provider API payloads trusted without validation,
truncation, encoding, numeric casts, max_length mismatches between layers.*

Repo HEAD `35e4230` (branch `main`). Report-only; no code changed. Confidence
labels per the methodology: `verified-by-trace` / `strong-inference` / `hypothesis`.

## Summary

The 2026-06-10 incident hardened **one** identifier path (the multi-tenant
*view-name* `{prefix}__{table}` composition in `schema_manager.build_view_schema`)
against PostgreSQL's 63-byte truncation. The same failure class survives, unfixed,
on **two sibling identifier paths that feed the same physical database**:

1. the **base tenant schema name** derived from a provider-supplied `external_id`
   (`_sanitize_schema_name`), and
2. **every dbt model name and column alias** generated from CommCare app metadata
   (`commcare_staging.py`), which become physical Postgres relations/columns.

Both trust an upstream string of up to 255 Django chars and emit it as a Postgres
identifier with no byte bound. Neither has the collision detection the view path got.

On the writer side, the missing-`id` crash class fixed for 5 Connect tables
(`2587158`) still has live siblings: the `visits` writer crashes on a missing id,
and several writers default primary keys to `""`, which silently collapses rows.

Lower-severity: NUMERIC(14,2) money columns trust provider magnitudes;
`_RESUMABLE_CONNECT_SOURCES` is now stale relative to the YAML `resumable` flags;
`_json_safe` (a Decimal→float coercion) is dead code.

---

## Findings

### F1 — Base tenant schema name is not byte-bounded: silent 63-byte truncation collision can share one physical schema across two tenants
**Status: LATENT · Impact: data-loss/security (cross-tenant) · Confidence: strong-inference · Complexity: accidental**

`SchemaManager.provision()` derives the physical schema name straight from the
provider's `external_id` with no length bound:

- `apps/workspaces/services/schema_manager.py:66` — `schema_name = self._sanitize_schema_name(tenant.external_id)`
- `:625-631` `_sanitize_schema_name` lowercases, maps `-`→`_`, strips to
  `[a-z0-9_]`, prefixes `t_` if it starts with a digit, and returns. **No
  truncation, no byte check.**
- `:99-103` then `CREATE SCHEMA IF NOT EXISTS {schema_name}` via
  `psycopg.sql.Identifier`. PostgreSQL silently truncates identifiers to 63 bytes
  (NAMEDATALEN-1) — `Identifier` quotes but does **not** prevent truncation.

Uniqueness is enforced only at the Django layer: `TenantSchema.schema_name =
CharField(max_length=255, unique=True)` (`apps/workspaces/models.py:33`). So two
tenants whose sanitized `external_id`s differ only **after** byte 63 produce two
distinct Django rows (unique constraint satisfied at 255 chars) but **one** physical
schema (`CREATE SCHEMA IF NOT EXISTS` finds the first tenant's truncated schema
already present). Both tenants then read/write the same `raw_*` tables → cross-tenant
data disclosure and clobbering.

Reachability: live for the CommCare provider, where `external_id` is the CommCare
**domain** — user-supplied free text that only has to match a real domain
(`apps/users/services/api_key_providers/commcare.py:69-73` returns
`TenantDescriptor(domain, domain)`; the OAuth path stores `domain["domain_name"]`
at `tenant_resolution.py:54`). Domains can be long. Connect (`str(opp_id)`, numeric)
and OCS (UUID, 32 hex chars) external_ids are short, so the collision is
provider-shaped, not universal — hence LATENT not BROKEN-NOW.

This is the **exact** bug the view layer was hardened against. Compare
`_view_prefix` (`:219-241`), which explicitly caps to 32 chars and appends a
sha256 digest for distinctness, plus `build_view_schema`'s byte-length check
(`:335`) and collision check (`:308-312`, `:338-343`). The base schema name — the
*input* to that very view machinery — got none of that.

Chain:
- entry: `provision()` `schema_manager.py:66`
- `_sanitize_schema_name` returns un-truncated name `:625-631`
- `CREATE SCHEMA IF NOT EXISTS {ident}` `:99-103` / `:141-144` (PG truncates to 63B)
- Django uniqueness at 255 chars passes for two distinct long names `models.py:33`
- consequence: two `TenantSchema` rows → one physical schema → shared `raw_*` tables.

---

### F2 — dbt model names and column aliases from CommCare metadata have no 63-byte guard: silent truncation collisions in the transform layer
**Status: LATENT · Impact: correctness (silent wrong data) · Confidence: strong-inference · Complexity: accidental**

`commcare_staging.py` builds dbt model names and SQL column aliases from
provider-controlled CommCare app metadata (form names, case-type names, question
paths). None are bounded to 63 bytes, yet each becomes a physical Postgres
relation or column:

- model names: `stg_case_{slug}` (`:147`), `stg_form_{slug}` (`:201`),
  `{parent_model}__repeat_{group_slug}` (`:248`) where `slug = slugify_model_name(name)`
  (`:48-65`) — `slugify` bounds *shape* (lowercase/underscore) but **not length**.
- `TransformationAsset.name = CharField(max_length=255, ...)` with a regex that
  validates shape only, not length (`apps/transformations/models.py:23-32`).
- `dbt_project.write_dbt_project` writes `models/{asset.name}.sql`
  (`apps/transformations/services/dbt_project.py:49-50`); dbt's default relation
  name is the model file name, materialized `+materialized: table` (`:44`) into the
  tenant schema. PostgreSQL truncates the relation name to 63 bytes.
- column aliases: `_unique_alias` (`:89-95`) disambiguates collisions **in Python
  before truncation**; e.g. `properties->>'<longprop>' AS "<longcol>"` (`:141`),
  form question aliases (`:195`), repeat-group aliases (`:239`). Two property names
  whose slugs share the first 63 bytes truncate to the same physical column →
  "column specified more than once" or a silently-merged column.

Two distinct long form/case names that share the first ~63 bytes of their slug
produce two `TransformationAsset` rows (unique at 255 chars) but one physical
relation; dbt builds one over the other or a `ref()` resolves ambiguously. The
2026-06-10 view-name fix did **not** touch this path — it is the same truncation
class one subsystem over.

Reachability: CommCare provider, transform phase, when system assets exist
(`materializer.py:217-234` generates them; `_run_transform_phase` runs dbt). Form
and case names are admin-controlled, not arbitrary end-user input, which caps the
realistic likelihood — LATENT.

---

### F3 — Residual missing-/empty-id writer hazards: `visits` crashes on a missing id; several writers default primary keys to `""` and silently collapse rows
**Status: LATENT · Impact: correctness + data-loss · Confidence: strong-inference · Complexity: accidental**

Commit `2587158` fixed the missing-`id` `NotNullViolation` for five Connect tables
by switching them to surrogate identity keys. Live siblings remain:

- **`visits` crashes on a missing id.** `_normalize_visit` maps
  `"visit_id": raw.get("id")` (`connect_visits.py:48`); the writer inserts it into
  `visit_id BIGINT PRIMARY KEY` (`materializer.py:1424,1462`). A visit row lacking
  `id` yields `None` → `NotNullViolation` → the whole `executemany` page fails →
  source FAILED. This is exactly the failure mode `2587158` describes for the other
  tables ("the production tenant-765 failure that left the run PARTIAL"), still
  present for visits because visits legitimately carry an id today — but the code
  trusts that invariant with no guard. (strong-inference; depends on a malformed
  upstream row.)

- **Empty-string default primary keys silently collapse distinct rows.** Multiple
  writers default the PK to `""` and rely on `ON CONFLICT (pk) DO UPDATE`:
  - Connect users: `username TEXT PRIMARY KEY`, insert `r.get("username", "")`
    (`materializer.py:1519,1548`), `ON CONFLICT (username) DO UPDATE`
    (`:1284`). Two users with missing/empty `username` collapse to one row.
  - OCS sessions: `session_id TEXT PRIMARY KEY`, `r.get("session_id", "")`
    (`:905,927`); experiments, participants, messages likewise use
    `*_id ... PRIMARY KEY` filled from `str(... or "")` in the loaders
    (`ocs_sessions.py:43`, `ocs_participants.py:67`, `ocs_experiments.py:24`).
  - CommCare cases/forms: `case_id`/`form_id TEXT PRIMARY KEY` with `r.get("...","")`
    (`materializer.py:1127,1155` / `:1194,1216`).

  Postgres treats `''` as a valid, single distinct key, so two id-less records
  upsert onto the same row — silent row loss with no error and a wrong count. The
  same `ON CONFLICT DO UPDATE` that makes replay idempotent also makes empty-id
  collisions invisible.

Net: the writer boundary trusts that every provider row carries a non-null,
non-empty natural key. Where it doesn't, the outcome is either a page-level crash
(visits) or silent under-counting (empty-string PKs).

---

### F4 — NUMERIC(14,2) money columns trust provider magnitude; an out-of-range value fails the whole source
**Status: LATENT · Impact: correctness/cost-perf · Confidence: hypothesis · Complexity: accidental**

Connect money fields are written into `NUMERIC(14, 2)` columns directly from the
JSON payload with no range check: `payment_accrued` (`materializer.py:1524`),
`amount`/`amount_usd` on payments (`:1696-1697`), invoices (`:1780-1781`),
completed_works `saved_*` accruals (`:1613-1616`). `NUMERIC(14,2)` caps at
999,999,999,999.99. A provider value at/above 10^12 (or a currency reported in
minor units) raises `NumericValueOutOfRange`, failing the entire `executemany`
page and marking the source FAILED. The precision/scale was chosen to "mirror the
Django model" per the docstrings (`:1507-1509`) — a claim about the upstream
serializer, not a validated bound on the wire value. Likelihood is data-dependent,
hence hypothesis.

---

### F5 — `_RESUMABLE_CONNECT_SOURCES` disagrees with the YAML `resumable` flags it duplicates (dual source of truth)
**Status: DEBT · Impact: velocity (foot-gun) · Confidence: verified-by-trace · Complexity: accidental**

Resumability is decided in `run_pipeline` from the pipeline config:
`source_is_resumable = is_resumable_provider and source.resumable`
(`materializer.py:264`). But `_load_connect_source` *also* branches on a hardcoded
set, `_RESUMABLE_CONNECT_SOURCES` (`:772-774`), which still lists
`completed_works, payments, invoices, assessments, completed_modules` — the exact
five that `2587158` made **non-resumable** (`pipelines/connect_sync.yml` sets
`resumable: false` for them). The two now disagree for 5 of 6 sources.

It is saved from being a live bug only because, when `source.resumable=False`,
`run_pipeline` passes `start_cursor=None`/`cursor_callback=None`, so the
"resumable" branch in `_load_connect_source` (`:803-812`) behaves like a clean
load. But the constant's own docstring ("Connect sources that the materializer
should drive in resumable mode") is now false, and flipping a YAML flag back to
`true` would re-enable a path the writers no longer support (these tables have no
integer keyset id). Two sources of truth for the same boolean. (verified-by-trace.)

Side note, same area: `_load_and_commit_source`'s docstring claims non-resumable
sources "run inside one transaction; commit once at the end"
(`materializer.py:689-690`), but the five non-resumable Connect writers call
`conn.commit()` per page internally (`:1656,1741,1815,1888,1955`). On a mid-source
failure, earlier pages are already committed despite the "single transaction"
contract. No corruption (next run drops+recreates), but a comment/code mismatch —
a claim, not a fact.

---

### F6 — `_json_safe` Decimal→float coercion is dead code (cannot cause precision loss, but masks intent)
**Status: COSMETIC/DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

`apps/artifacts/views.py:749-761` defines `_json_safe`, which coerces `Decimal`→
`float` (lossy for money). It has **zero callers** in `apps/` or `mcp_server/`
(grep). The live artifact query path (`ArtifactQueryDataView`, `:773-843`) returns
psycopg rows straight into `JsonResponse`, whose default `DjangoJSONEncoder`
renders `Decimal`→str and datetimes→isoformat, so there is no live precision loss.
Worth flagging because the function reads as the boundary serializer and a future
caller wiring it in would silently introduce float precision loss on NUMERIC(14,2)
money. (verified-by-trace: definition present, no usages.)

---

## What's fine (verified healthy in this lens)

- **View-name composition is correctly hardened.** `build_view_schema`
  (`schema_manager.py:243-447`) bounds the per-tenant prefix to 32 chars with a
  deterministic sha256 digest (`_view_prefix:219-241`), checks the final composed
  name's **byte** length against 63 (`:335`), and hard-fails on both prefix
  collisions (`:308-312`) and full view-name collisions (`:338-343`) before any
  DDL. The view schema name itself is bounded and regex-validated
  (`_view_schema_name:214-217`, `:294`). This is the model the other identifier
  paths should follow.
- **SQL identifiers are parameterized correctly.** Every dynamic schema/table/role
  name in `schema_manager.py`, `query.py`, and `materializer.py` uses
  `psycopg.sql.Identifier`/`SQL().format`, not f-string interpolation; the
  `commcare_staging.py` literal interpolation is guarded by `_sql_escape`
  (single-quote doubling) and `slugify_model_name` shape validation. No SQL
  injection found in this lens.
- **Connect pagination resume is idempotent for the tables that carry a keyset id.**
  `_max_id` skips non-int ids (`materializer.py:1237-1247`); visits' `ON CONFLICT
  (visit_id) DO UPDATE` makes page replay safe; the five id-less tables were
  correctly moved to surrogate identity keys + non-resumable. The cross-DB
  atomicity gap (`f26c1a0`) is genuinely closed for the resumable set.
- **`_load_prior_resume_cursors`** correctly refuses to resume across an
  intervening COMPLETED run (`materializer.py:564-605`), preventing the
  duplicate-insert regression it documents.
- **Auth-header builders** (`commcare_base.build_auth_header`, `ocs_base`,
  `connect_base`) and the 401/403 → typed-error handling are consistent across all
  three providers.
- **OCS `_map_session`/`_map_participant`** defensively coerce nested
  `experiment`/`participant` objects to ids (`ocs_sessions.py:36-42`) and default
  missing fields — no KeyError surface.
- **Pagination `next`-URL handling** is provider-correct: CommCare resolves
  relative URLs via `urljoin` (`commcare_base.py:49-59`), Connect pins the
  http→https redirect behavior with a regression test, and the loop terminates on
  `next is None`.

---

## Coverage log (honest)

### Deep-read (line-by-line)
- `mcp_server/services/materializer.py` (all 1973 lines: orchestrator + all writers)
- `mcp_server/services/schema_manager.py` (`apps/workspaces/services/`)
- `apps/users/services/tenant_resolution.py`
- `apps/users/services/api_key_providers/commcare.py`, `base.py` (signatures)
- `mcp_server/loaders/`: connect_base, commcare_base, ocs_base, connect_visits,
  connect_users, connect_payments, connect_assessments, connect_metadata,
  connect_completed_works, connect_invoices, connect_completed_modules,
  commcare_forms, commcare_cases, commcare_metadata, ocs_sessions, ocs_messages,
  ocs_participants, ocs_experiments, ocs_metadata (all 19 + 3 bases)
- `mcp_server/services/metadata.py`, `mcp_server/services/query.py`
- `mcp_server/pipeline_registry.py`, `pipelines/connect_sync.yml`
- `apps/transformations/services/commcare_staging.py`, `executor.py`, `dbt_project.py`
- `apps/artifacts/views.py` (query-data/data/`_json_safe` region, ~720-850)
- `apps/transformations/models.py` (name field), `apps/users/models.py`
  (TenantMembership/TenantConnection), `apps/workspaces/models.py` (max_length scan)
- git: `f26c1a0`, `2587158` (full diffs); `5421344` (resumable-set introduction)

### Skimmed
- `apps/users/views.py` (connections/api-key CRUD — scanned for casts/truncation,
  not fully traced)
- `apps/users/services/api_key_providers/ocs.py`, `registry.py` (not opened in full)
- All `apps/*/models.py` `max_length` declarations (grep-level, not each consumer)
- `apps/artifacts/services/export.py` (function inventory + numeric grep only)

### NOT examined (in-scope, left for gap loop)
- `mcp_server/services/sql_validator.py` (401 LOC) — the `query` tool allow-list,
  LIMIT injection, and `_get_limit_value` numeric cast. Touched only via `query.py`
  call sites; the validator's own length/numeric handling is **uninspected**.
- `mcp_server/context.py` / `mcp_server/server.py` — `workspace_id`/`tenant_id`
  shape validation at the MCP tool boundary (the highest-fix-density file per
  cartography) was not opened for this lens.
- `apps/chat/views.py` / `stream.py` — `workspace_id`/`thread_id` parsing from the
  streaming request body not validated here.
- `apps/recipes/services/runner.py` — `prompt_template` interpolation and any
  numeric/identifier handling in templated re-invocation.
- `apps/knowledge/` — `TableMetadata.table_name` free-text key, `Learning` SQL
  fields: shape/length validation on import/export not examined.
- Frontend: hand-written TS types vs API shapes; **BIGINT → JS number precision**
  for `visit_id`/identity keys serialized to the artifact sandbox was noted as a
  possibility but not traced (`frontend/src/api/*`, ChartRenderer). Left open.
- `apps/users/services/merge.py` — email/identifier handling in the merge path
  (flagged elsewhere; not this lens's focus, not opened).
- Runtime confirmation: none of these are demonstrated against a live DB; all
  identifier-truncation findings are static-trace + PostgreSQL-semantics inference,
  not reproduced. F1/F2 would be worth a single psql repro in the gap loop.
