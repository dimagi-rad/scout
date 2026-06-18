# Auto-modeled Cube semantic layer over Connect data

**Status:** Design — approved for planning
**Date:** 2026-06-18
**Author:** Jonathan Jackson (with Claude)

## 1. Context & motivation

Today, when a Scout user asks a question, the LangGraph agent free-writes SQL against
whatever materialized tables exist. This has three structural weaknesses:

1. **Metric definitions live nowhere durable.** "MUAC confirmation rate" is re-derived
   per chat and can be subtly wrong or inconsistent between answers.
2. **No caching.** Every question is a live warehouse scan.
3. **The LLM's query surface is the entire raw schema** — large, ambiguous, easy to get
   wrong (bad joins, wrong grain, fan-out double-counting).

We want to evaluate adopting **[Cube](https://cube.dev) (Core, open-source)** as a
**semantic layer**: a governed, version-controlled definition of metrics, dimensions, and
relationships that the agent queries *by name* instead of writing raw SQL. Cube does not
store a copy of the data — it sits in front of Scout's existing managed Postgres, compiles
governed queries to correct SQL (or serves them from pre-aggregation cache), and exposes
them over a Postgres-wire **SQL API**.

This is a strong fit because **Scout has already hand-built ~60% of what Cube is** (an MCP
query surface, an informal knowledge layer, materialization + dbt staging, multi-tenant
access control). Cube is a principled replacement for the missing pieces — *governed metric
definitions* and *caching* — and the SQL API slots into Scout's existing psycopg query path.

The Cube model also directly serves a standing goal: **persist knowledge over time.** The
version-controlled model becomes the durable, blessed crystallization of what Scout's
knowledge layer (`KnowledgeEntry`, `TableKnowledge`, `AgentLearning`) accumulates informally.

### Primary question this work answers

> Is a governed semantic layer worth adopting for Scout — does querying governed metrics
> produce more correct/consistent agent answers than free-SQL, and is the modeling cost
> acceptable (can the model be **auto-generated** rather than hand-authored)?

To answer it credibly we build the whole spine, with the early phases doubling as the
evaluation harness.

## 2. Goals / non-goals

**Goals**
- Get synthetic Connect data into Scout as a real data source (test fuel).
- **Auto-model** a Cube semantic layer from Connect/CommCare **app structure** (form schemas)
  + Scout's existing knowledge — not hand-written YAML.
- Let the agent query governed measures, falling back to raw SQL when unmodeled (seamless to
  the user — "it's just magic").
- Produce an **evaluation** comparing free-SQL vs via-Cube answers → an adoption verdict.
- Build the **self-improving loop** that grows the model from usage (persist knowledge).

**Non-goals**
- Cube Cloud / hosted MCP / D3 (we use Cube Core in Docker).
- Replacing raw-SQL access wholesale — Cube is additive; we migrate domains incrementally.
- Exposing Cube or any modeling concept to end users. Users only ever chat.
- Real (non-synthetic) Connect ingestion changes beyond what reuse naturally provides.

## 3. End-state architecture

```
connect-labs /api/export/  (incl. app_structure)            [external dep: connect-labs#637]
      │   Scout Connect loader, re-pointed at synthetic base URL + PAT
      ▼
managed Postgres:  raw_visits, raw_users, raw_completed_works, raw_completed_module
      +  TenantMetadata.metadata.app_structure   ← deliver-app form schema (semantic source)
      │
      │   AUTO-MODELING ENGINE  (staged schema + form definitions + Scout knowledge)
      ├──►  enriched dbt staging   (flatten form_json → typed/labeled cols; repeats → child
      │        tables; choice lists → enum dims)  +  auto-populated TableKnowledge.column_notes
      └──►  generated Cube model   (cubes / views / measures)  →  committed to  cube/  (durable)
      │
   Cube Core (Docker)  over managed Postgres  ──exposes──►  SQL API (pg-wire)
      │
   Scout agent  ──prefers governed measures via semantic_query; falls back to raw SQL──►  chat
      │
   EVAL framework   golden questions answered free-SQL vs via-Cube → scored = adoption verdict
      │
   SELF-IMPROVING LOOP   AgentLearning + unmodeled-question detection → proposed measures
                          → curation gate → merged into cube/ → redeploy
```

## 4. Components

### 4.1 Connect-labs synthetic source (M1)

**Reuse first.** Scout already has a Connect pipeline (`pipelines/connect_sync.yml`,
provider `commcare_connect`) and Connect loaders incl. a `ConnectMetadataLoader`, plus a base
loader (`mcp_server/loaders/commcare_base.py`) that already supports OAuth Bearer **and** API
key/PAT auth and TastyPie-style pagination. The synthetic source is the *same Connect export
shape* at a different base URL with a different token.

Work:
- **Credential / connection:** add a connection type for the connect-labs synthetic API —
  base URL + MCP PAT, stored via the existing Fernet-encrypted `TenantConnection`
  (`apps/users/models.py`) and resolved by `credential_resolver`. (PAT minted at
  `connect-labs /labs/mcp/tokens/`.)
- **Pipeline:** `pipelines/connect_labs_sync.yml` (or parameterize `connect_sync.yml` with a
  base URL) covering sources `visits`, `users`, `completed_works`, `completed_module`.
  Pagination envelope is `{results, next, count}`; honor `?page_size=`.
- **app_structure fetch (the key gap):** extend the Connect metadata loader to call
  `GET /api/export/opportunity/{id}/app_structure/` and store the deliver-app form schema into
  `TenantMetadata.metadata` (same JSONB slot CommCare app structure uses), normalized to the
  `form_definitions` shape (`questions: [{label, value, type, repeat, options?}]`).
- **Discovery:** use `GET /api/export/opportunities/` to enumerate accessible synthetic opps.

Acceptance: a synthetic opp materializes `raw_*` tables into the managed schema and its
deliver-app form schema lands in `TenantMetadata.metadata`.

> **Decoupling:** M1 can develop against a recorded fixture/stub of the #637 API so Scout work
> is not blocked on connect-labs landing.

### 4.2 Auto-modeling engine (M2 + M3 output) — the heart

Inputs: the materialized `raw_*`/staged schema, the deliver-app **form definitions** (labels,
types, choice lists, groups, repeat flags), and Scout's existing knowledge
(`KnowledgeEntry`, `TableKnowledge`, `AgentLearning`).

**(a) Enriched staging — improve, don't replace.**
`apps/transformations/services/commcare_staging.py` already generates dbt staging from form
definitions but skips the semantic richness. Extend it to:
- Flatten `form_json` / case `properties` into **typed, human-labeled** columns driven by the
  question schema (e.g. `muac` → `Decimal`, labeled "MUAC measurement (cm)", group `muac_group`).
- Materialize **repeat groups** into child tables (currently skipped at
  `commcare_staging.py:186`).
- Turn **choice/option lists** (`Select`/`MultiSelect`) into enum/lookup dimensions.
- **Auto-populate `TableKnowledge.column_notes`** (today manual-only) from question
  labels/types — this immediately improves the *raw-SQL* agent via `KnowledgeRetriever`, and
  lifts the eval baseline.
- Optionally enrich `metadata.py::_build_jsonb_annotations` to map columns → question labels.

**(b) Cube model generation.**
A generator turns staged schema + form semantics + knowledge into Cube YAML:
- **Dimensions** from labeled columns (type-mapped; choice lists → categorical dims).
- **Joins** from the pipeline's declared `RelationshipConfig`s (correct cardinality).
- **Measures** seeded from: known KPI patterns (the synthetic manifest's `kpi_config` is a gift
  here — `muac_confirmation_rate`, approval/flag rates), Scout `KnowledgeEntry` metric
  definitions, and golden queries. LLM-assisted, but every measure is reviewable YAML.
- **Views** (e.g. `program_health`) curate query-ready surfaces over the cubes.
- Output committed to `cube/model/*.yml` (version-controlled = the durable knowledge asset).

A `TransformationAssetRun`-style record tracks each generation run.

### 4.3 Cube Core deployment (M3)

- Add a `cube` service to `docker-compose` (and dev `Procfile`), reading `cube/model/`,
  configured against `MANAGED_DATABASE_URL` + the synthetic schema (`search_path`).
- Expose the **SQL API** (Postgres wire). Pre-aggregations optional — include a toggle so the
  eval can measure the caching win.
- Multi-tenancy: scope Cube to the workspace's schema (mirrors Scout's existing schema routing).

### 4.4 Agent ↔ Cube integration — interface 1, SQL API (M3)

The 1-vs-2 interface choice is **invisible to users**; we pick the lowest-friction one.
- New MCP tool **`semantic_query`** that connects to Cube's pg-wire SQL endpoint (parallel to
  the existing raw `query` tool in `mcp_server/server.py`), reusing the validation/LIMIT/
  read-only patterns where applicable.
- New **`semantic_catalog`** tool (or prompt injection) exposing available measures/dimensions
  so the agent knows what it may ask for.
- **Routing/fallback:** the agent prefers `semantic_query` for governed metrics and falls back
  to raw `query` when a question isn't modeled — so the user never sees a gap. Every fallback
  is recorded as an "unmodeled question" signal for the self-improving loop (4.6).

### 4.5 Evaluation framework (M4) — the adoption verdict

New `apps/evals` (no existing eval framework in Scout today):
- **Models:** `GoldenQuery` (title, NL question, reference intent/SQL, expected-result summary,
  source) and `EvalRun` (per question: free-SQL path result + Cube path result, each run N
  times; equivalence verdict, consistency/variance, latency).
- **Runner:** answers each golden question both ways via the agent, deterministically compares
  result sets, and uses an LLM judge for semantic equivalence; aggregates a scorecard
  (correctness, consistency across runs, latency, and # questions Cube could answer at all).
- **Seed set:** ~8–15 representative questions over the synthetic MUAC/CHW domain (counts,
  rates by archetype/week, flagged-visit analysis, payment reconciliation).
- Output: a report that is the **go/no-go evidence** for adoption.

### 4.6 Self-improving loop + curation (M5) — persist knowledge over time

- **Detect** model gaps: fallback-to-SQL events (4.4) + mined `AgentLearning` corrections
  (join patterns, aggregations, type fixes).
- **Propose** new measures/dimensions (LLM) as candidate Cube YAML diffs.
- **Curation gate:** a human approves before merge — governance is the whole point; auto-merging
  unreviewed metrics would destroy the correctness guarantee. (Mechanism: lightweight review UI
  or PR-based; decided in planning.)
- **Apply & redeploy:** approved candidates merged into `cube/`, Cube reloads.
- The loop closes the knowledge story: `KnowledgeEntry`/`AgentLearning` are the raw accumulating
  knowledge; the Cube model is its blessed, governed form.

## 5. Build sequence (one plan, five phases)

| Phase | Delivers | Reuse / build | Independent value |
|---|---|---|---|
| M1 | Connect-labs loader/pipeline → `raw_*` + `app_structure` | Reuse Connect loader/creds/pipeline; add app_structure | Synthetic Connect data in Scout |
| M2 | Enriched auto-staging + auto `column_notes` | Improve `commcare_staging.py` | Better answers *without* Cube; lifts eval baseline |
| M3 | Cube Core + generated model + `semantic_query`/`semantic_catalog` | New + generator | Governed-metric querying |
| M4 | Eval framework: free-SQL vs Cube | New (`apps/evals`) | **Adoption decision, with data** |
| M5 | Self-improving loop + curation gate | New | Persist knowledge over time |

## 6. External dependency

- **connect-labs#637** — expose authenticated `/api/export/` endpoints serving synthetic data,
  including a generator-emitted `app_structure.json`. Tracked separately; Scout develops M1
  against a recorded fixture until it lands.

## 7. Risks & open questions (resolve during planning)

- **Cube model generation quality** — does LLM-generated YAML produce correct measures? Mitigated
  by reviewable YAML + the eval harness as a feedback signal.
- **Semantic SQL reliability** — if the agent fumbles `MEASURE()` SQL (interface 1), the fallback
  is interface 2 (structured `cube_query` tool). Eval will reveal whether this is needed.
- **Repeat-group flattening** — child-table modeling and how Cube joins to them needs care
  (grain/fan-out).
- **Cube as a hot-path runtime dependency** — new service to run/deploy/keep healthy; weigh in
  the adoption verdict.
- **Curation-gate mechanism** (UI vs PR-based) — decide in planning.
- **Pre-aggregations** — in/out of the POC scope for measuring caching benefit.

## 8. Testing

- Unit: loader pagination/auth, app_structure normalization, staging flattening (typed/labeled
  cols, repeats, choice lists), `column_notes` auto-population, Cube-model generator output shape.
- Integration: end-to-end materialize → stage → generate model → `semantic_query` returns
  governed metric; fallback path engages on unmodeled question.
- Async tests follow Scout conventions (`AsyncClient`, `transaction=True`).
- The eval framework is itself a test of the thesis (not a unit test, but the decision artifact).
