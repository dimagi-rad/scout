# Content-satellite redesign — recipes + knowledge + artifacts

**Date:** 2026-07-03
**Issue:** arch #267 — "[DESIGN] Content-satellite redesign: recipes + knowledge + artifacts" (cluster `content-satellite-redesign`, wave 2, `design-gated`, `tier:design`). Folds in the **#241 residual** (workspace-scope transforms never materialize) and relates to **#318** (learning-lifecycle).
**Status:** Design — awaiting Brian's sign-off. **No implementation until approved.** This is a *product-shape* decision as much as an architecture one.
**Author:** design window (research: 3 read-only mapping passes over recipes / knowledge / artifacts)

## Problem

Wave-1/2 fixes restored recipes, knowledge, and artifacts to **working-as-designed** (recipe runner async-first, headless materialization, `TableKnowledge` re-keyed to logical names, artifact multi-tenant render, sandbox isolation). #267 asks the separate question: **is the current design what we want?** Three "content satellites" grew independently and now share nothing but accidental structure, while the cross-cutting concerns they *should* share (sharing/privacy, provenance, drift-integrity, lifecycle) are each solved differently or not at all.

The concrete symptoms that make this a redesign rather than more point-fixes:

1. **Three satellites, zero shared spine.** `Recipe`, `Artifact`, `TableKnowledge`/`KnowledgeEntry`/`AgentLearning` are all workspace-scoped, all soft-deletable, all attributed to a `created_by` user — and each re-implements soft-delete, sharing, and provenance from scratch, inconsistently. There is **no coupling between the three apps** (grep-confirmed: recipes import neither knowledge nor artifacts; artifacts import none of them). Coupling is inverted and accidental: `apps/chat/thread_views.py` and the agent tools reach *into* artifacts.

2. **Sharing/privacy is three different models.** `Recipe` and `RecipeRun` each carry `is_shared` + `is_public` + auto-minted `share_token` (`recipes/models.py:55-70,294-336`), with a live `AllowAny` public route `PublicRecipeRunView` (`recipes/api/views.py:204-220`). `Artifact` has **no per-artifact sharing** — it's exposed publicly only transitively through a shared chat thread, and then **source-only, never live data** (`chat/thread_views.py:53-64`). Knowledge has no sharing at all. A user cannot reason about "who can see this" consistently across the three.

3. **Provenance is a loose string.** `Artifact.conversation_id` is a `CharField(255)` matched against a thread id (`artifacts/models.py:84`), not an FK — `Artifact.objects.filter(conversation_id=str(thread_id))`. A recipe that produces an artifact records the linkage only inside `RecipeRun.step_results[0]["artifacts_created"]` (extracted from tool messages, `runner.py:144-159`). There is no first-class "this content came from this thread / this recipe / against this tenant's data" record — which is exactly what sharing, revocation (#249), and drift-detection all need.

4. **The #241 residual: workspace-scope transforms never materialize.** `TransformationScope` has `SYSTEM/TENANT/WORKSPACE` (`transformations/models.py:9-12`), but `_run_transform_phase` calls `run_transformation_pipeline` **without a workspace** (`materializer.py:1080`), and the executor only appends the WORKSPACE stage when a workspace is passed (`executor.py:67-74`). So `scope=WORKSPACE` assets are creatable via API but run **only** through the manual `/runs/trigger/` endpoint (no UI) — never during materialization. The documented root cause (`materializer.py:1067-1079`) is structural: **materialization is tenant-scoped, `Workspace↔Tenant` is M2M (`WorkspaceTenant`), so there is no single workspace context at materialize time.**

5. **The same scope mismatch drives the `sync_column_notes` fan-out.** Knowledge is **workspace-scoped**; materialization is **tenant-scoped**. So when materialization wants to write column notes it must fan **one tenant → all of that tenant's workspaces** (`materializer.py:255-256`, commcare_connect only). The #241 gap and the column-notes fan-out are the **same root defect** viewed from two directions.

6. **The learning lifecycle (#318) is inert.** `AgentLearning` advertises an adaptive loop in its `help_text`, but `increase_confidence`/`decrease_confidence` are called only from Django admin (`knowledge/models.py:186-196`, `admin.py:162-173`); `times_applied` bumps only on a byte-identical `iexact` re-save (`learning_tool.py:158-161`) — never for LLM prose; drifted learnings never retire. The retriever renders "(applied N times)" implying usage that isn't tracked.

7. **Knowledge retrieval is a stable-order dump, not retrieval.** `KnowledgeRetriever` ignores its `user_question` ("for API compatibility only; no relevance index", `retriever.py:47-51`) and concatenates entries in stable order into a 6000-char budget inside the **cacheable prompt prefix** (`base.py:801-834`, `_SYSTEM_PROMPT_TTL=60s`). As a workspace's knowledge grows past the budget, later entries silently fall out with no relevance ranking.

8. **CLAUDE.md advertises models that don't exist.** The knowledge app is described as holding "golden queries, eval runs" — **no such model exists** (grep-confirmed; `KnowledgeEntry` replaced the former `VerifiedQuery`/`CanonicalMetric`/`BusinessRule`). Either build them or correct the description.

## Goals

- Decide the **target shape** for user- and agent-generated content: repair three separate satellites, or give them a shared spine. Get an explicit product call from Brian.
- Whatever the shape: make **sharing/privacy one model**, make **provenance first-class**, and make **drift-integrity** (content referencing tables/columns that may change) a designed property rather than an accident.
- Resolve the **#241 workspace-transform scope question** with an explicit answer, not another "documented residual."
- Give the **learning lifecycle (#318)** a real capture→apply→retire loop, designed alongside (not bolted onto) knowledge.
- Reconcile the docs with reality (golden queries / eval runs).

## Non-goals (explicit)

- **Recipe runtime.** Already redone: async-first runner + headless `interactive=False` graph + background `run_recipe` task + 202/poll (see `2026-06-16-recipes-async-restoration-design.md` and the `2026-06-17-recipe-headless-materialization` plan, both implemented). #267 is about the **content/data model and lifecycle**, not execution.
- **The tenancy/permission substrate (#249/#250).** Fixed input (`2026-06-18-tenant-access-refresh-design.md`). #267 *consumes* the centralized authorizer and the live-tenant rule; it does not redesign them. But it must **surface** where the current content sharing model bypasses them (below).
- **Rewriting the dbt confinement / low-privilege execution** — that part of #241 shipped (`executor.py:140-149`, `TransformStageError`).
- **The status/catalog consolidation (#251)** — its own spec (`2026-07-03-status-catalog-module-design.md`). #267 depends on #251's canonical **logical-name catalog** as the drift-integrity anchor (below).

## Current-state map

### Recipes (`apps/recipes/`)
- `Recipe`: workspace-scoped (nullable FK), `prompt` TextField with `{{var}}` placeholders + `variables` JSON, `created_by`, soft-delete trio, `is_shared`/`is_public`/`share_token`. `RecipeStep` model is **vestigial** (runner executes single `Recipe.prompt`; admin doc confirms, arch #260).
- `RecipeRun`: `step_results` JSON (single-element list: prompt/response/tools_used/artifacts_created/timestamps), its own `is_shared`/`is_public`/`share_token`, `run_by`.
- Runtime (done): `recipe_run_view` → 202, `run_recipe` Procrastinate task (`queue="recipes"`), `RecipeRunner.execute_async` builds `interactive=False` headless graph. A run *may* invoke the blocking `run_materialization` tool.
- Public: `PublicRecipeRunView` (`AllowAny`, `authentication_classes=[]`) serves a run by `share_token`. **`RecipeRunDetailView` has PATCH only, no GET** — yet the runtime docs say clients poll `GET .../runs/<id>/`; only the list route has GET (a doc/impl gap to confirm).

### Artifacts (`apps/artifacts/`)
- Single polymorphic `Artifact` (`ArtifactType`: react/html/markdown/plotly/svg): `code` (source), `data` (static config JSON), `source_queries` (list of `{name,sql}`), `version` + `parent_artifact` chain, `conversation_id` **CharField** (loose thread join), workspace-scoped, soft-delete.
- **Live, not snapshot:** the agent is instructed *not* to embed results; `ArtifactQueryDataView` re-runs `source_queries` at render (60s cache, `asyncio.gather`) through `load_workspace_context` (multi-tenant → `ws_*` view schema).
- Created by the agent mid-chat via `create_artifact`/`update_artifact` tools (`artifacts/tools/artifact_tool.py`); `update` creates a new versioned row.
- Render: server `sandbox/` HTML template with per-request CSP nonce, embedded in an `<iframe sandbox="allow-scripts allow-modals">` (**no `allow-same-origin`** by design). Public path (shared thread) renders html/svg only, react/plotly as source `<pre>` — live tenant data never served publicly.
- No per-artifact share token; export html works, png/pdf 501.

### Knowledge (`apps/knowledge/`)
- `TableKnowledge`: workspace-scoped, keyed on **logical** `table_name` (re-keyed off physical schema by #262 migration `0003`), annotations (`description`, `use_cases`, `column_notes`, `related_tables`, …); managed via the **workspaces data-dictionary API**, not the knowledge API; `column_notes` auto-populated during materialization for commcare_connect via `sync_column_notes`.
- `KnowledgeEntry`: title + markdown `content` + `tags`; zip export/import (import has correctness gaps per #262, mostly shipped).
- `AgentLearning`: agent-created via `save_learning`; `confidence_score`/`times_applied`/`is_active`; inert lifecycle (#318).
- `KnowledgeRetriever`: dumps entries + table context + top-20 learnings into the cacheable prompt prefix, stable-order, 6000-char budget, no relevance retrieval.
- **No golden-query / eval-run models** anywhere.

### The shared shape (what could be a spine)
All three are: workspace-scoped (nullable FK) · soft-deletable (`is_deleted`/`deleted_at`/`deleted_by` + a soft-delete manager) · attributed (`created_by`) · UUID PK · potentially shareable · reference workspace data that can drift. They differ in: cardinality (recipe→runs, artifact→versions, knowledge→annotations/learnings), whether they store source vs snapshot, and their sharing surface.

## Proposed design

Because #267 is explicitly a "brainstorm → spec before committing to repair-in-place vs rethink," the core deliverable is the **decision framing** plus a recommended phased path. Three coherent options:

### Option A — Repair-in-place, keep three satellites (minimal)
Keep the three apps and models. Fix only the concrete gaps: consistent sharing helpers, #241 answer, #318 loop, docs reconciliation. No shared base model.
- **Pro:** smallest blast radius; each fix is a small PR; no migration risk.
- **Con:** leaves three divergent implementations of sharing/soft-delete/provenance; the next feature re-forks them; "who can see this" stays inconsistent.

### Option B — Shared content spine, models stay separate (recommended)
Keep three distinct models (their lifecycles genuinely differ) but extract the **cross-cutting concerns** into one shared spine used by all three:
- **`ContentBase` mixin / abstract model**: UUID PK, `workspace` FK, `created_by`, soft-delete, timestamps — one implementation, one soft-delete manager, one admin pattern.
- **One sharing/ACL model**: a single `Share`/visibility concept (private / workspace / public-token) applied uniformly to recipes, runs, artifacts, and knowledge entries — replacing the two bespoke `is_shared`/`is_public`/`share_token` copies and giving artifacts the per-artifact sharing they lack. **Routed through the #250 permission layer + the #249 authorizer** so a public token still respects live-tenant revocation (closes the `PublicRecipeRunView`/public-thread `AllowAny` residual R2 from the tenant design).
- **First-class provenance**: replace `Artifact.conversation_id` CharField with a real provenance record (thread FK / recipe FK / tenant(s) the data came from), shared by artifacts and recipe runs. Enables drift-detection and correct revocation.
- **Drift-integrity against #251's logical catalog**: content that names tables/columns (artifact `source_queries`, knowledge `table_name`/`related_tables`, learning `applies_to_tables`) references the **canonical logical-name catalog** from #251, so drift is *detectable* (and, for learnings, a retirement trigger).
- **Pro:** one mental model for sharing/provenance/drift; artifacts gain sharing; tenancy holes close; each satellite keeps its natural lifecycle.
- **Con:** touches all three apps + a migration to move `conversation_id`/sharing fields onto the spine; must land carefully alongside #249/#250.

### Option C — Full unification into one `Content` model (rethink)
Collapse artifacts/knowledge-entries/recipes into a single polymorphic `Content` table with a `kind` discriminator and per-kind payload.
- **Pro:** maximal reuse; one API, one sharing model, one export/import.
- **Con:** the three lifecycles (versioned live-query code / prompt-template + runs / curated annotations + agent learnings) are different enough that a single table becomes a bag of nullable columns; large migration; high risk; overfits a "they look similar" observation. **Rejected** unless Brian wants a ground-up content platform.

**Recommendation: Option B**, phased so each phase is independently shippable and the tenancy-sensitive pieces (sharing/provenance) land in coordination with the #249/#250 window.

### The #241 workspace-transform fork (must be decided — folded into #267)
The M2M scope mismatch means "run workspace-scoped transforms during materialization" is genuinely ill-defined today. Options:
- **A1 — Run the WORKSPACE stage after the view schema is built**, per workspace, not per tenant. The view schema *is* the single workspace context that's missing at tenant-materialize time. Workspace transforms would build on top of the unioned `ws_*` views. Cleanest fit for the multi-tenant model.
- **A2 — Drop `scope=WORKSPACE` transforms entirely.** They have no UI, never run in the shipped product, and orphan/stale silently. If nobody uses them, delete the scope and the dead executor stage.
- **A3 — Keep manual-trigger-only, add a UI + staleness marker.** Lowest architectural change; keeps the footgun that a workspace-scoped table survives raw reloads with stale contents.
- *Recommendation:* **A2 unless there's a live use case**; if there is, **A1** (build workspace transforms on the view schema post-build). This is a Brian call — it decides whether "workspace transforms" is a product feature at all.

### The #318 learning-lifecycle design (alongside #267)
Design the loop, don't bolt it on:
- **Application-counting seam** on the *real* agent path — increment `times_applied` when a learning is actually injected *and* the subsequent query succeeds, not on `iexact` re-save.
- **Confirm/contradict signals** from the runtime: a query that succeeds after applying a learning → `increase_confidence`; a contradicting correction → `decrease_confidence`.
- **Retirement:** age out / deactivate learnings whose `applies_to_tables` no longer exist in the #251 logical catalog, or whose confidence decays below a floor.
- Reconcile the `help_text` and the retriever's "(applied N times)" with whatever ships.

## Data-model / migration implications

- **Option B spine:** an abstract `ContentBase` is code-only (Django abstract models add no table). The **sharing model** is either a new `Share` table (FK to the content object, generic or per-type) or shared fields promoted onto the spine — a migration either way, but additive. The **provenance** change replaces `Artifact.conversation_id` (CharField) with an FK/record — a data migration to backfill existing artifacts by matching the current string join (`conversation_id == thread.id`), then drop the CharField.
- **#241 A2 (drop WORKSPACE scope):** migration to delete `scope=WORKSPACE` `TransformationAsset` rows (audited) + remove the enum value + the executor stage. A1 (run post-view-build) is code + a new materialization phase, no schema change.
- **#318:** likely no new columns (fields exist); a possible `retired_at`/`last_applied_at` addition. Mostly wiring.
- **Docs:** correct CLAUDE.md's "golden queries, eval runs" (they don't exist) — or make them real (Decision below).

## Phased implementation plan

Each phase independently shippable; tenancy-sensitive phases gated on #249/#250 landing.

**Phase 0 — Reconcile docs + delete vestigial `RecipeStep`** (arch #260) and confirm/fix the `RecipeRunDetailView` GET gap. Pure cleanup, no risk. Correct CLAUDE.md's golden-query/eval-run description.

**Phase 1 — `ContentBase` spine (code-only).** Extract the shared soft-delete/attribution/timestamp mixin; migrate the three models to inherit it with **no behavior change** (abstract model → no migration). Golden test: existing soft-delete/list behavior unchanged.

**Phase 2 — Unified sharing/ACL, routed through #249/#250.** One visibility model across recipes/runs/artifacts/knowledge; give artifacts per-artifact sharing; make every public-token path re-check the authorizer so revoked tenants lose access (closes R2). **Gated on the tenant-access authorizer being merged.** This is the phase that most needs to co-land with the tenancy window.

**Phase 3 — First-class provenance.** Replace `Artifact.conversation_id` with a provenance record (thread/recipe/tenant); backfill; recipe runs record artifact provenance structurally, not via `step_results` string extraction. Unlocks drift-detection and correct revocation.

**Phase 4 — #241 resolution** (A1 or A2 per Decision). If A2: audited deletion + enum/stage removal. If A1: new post-view-build workspace-transform phase + staleness markers.

**Phase 5 — #318 learning lifecycle.** Application-counting seam + confirm/contradict wiring + retirement against the #251 catalog. Reconcile help_text/retriever text.

**Phase 6 (optional) — knowledge retrieval quality.** If knowledge volume justifies it, replace the stable-order dump with relevance retrieval (keeps the 6000-char budget honest as workspaces grow). Own decision below.

## Test strategy

- **Spine (Phase 1):** golden-master that soft-delete/list/attribution behavior is byte-identical across the three apps post-refactor.
- **Sharing (Phase 2):** matrix — private/workspace/public × member/non-member/anonymous × live-tenant/revoked-tenant — asserting a revoked tenant loses access even via a previously-minted public token.
- **Provenance (Phase 3):** backfill correctness (every existing artifact's new provenance matches its old `conversation_id` join); recipe-produced artifacts link structurally.
- **#241 (Phase 4):** A2 — no `WORKSPACE` asset survives; A1 — a workspace transform builds on the view schema and is marked stale when inputs reload.
- **#318 (Phase 5):** `times_applied` increments on real application; confidence moves on confirm/contradict; a learning whose tables vanish from the #251 catalog retires.
- Async DB tests: `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)`; new interactive UI elements get `data-testid` per `CLAUDE.md`.

## Cross-cutting collisions

- **#249/#250 (tenancy/permission, live window — fixed input).** Phase 2 (sharing) and Phase 3 (provenance) are the collision surface: the unified sharing model **must** be built on the centralized authorizer, and every `AllowAny` public path (`PublicRecipeRunView`, public thread → artifacts) must re-check live-tenant access — this is the R2 residual the tenant design explicitly deferred. **Do not design the sharing model in isolation from that window.** Provenance's "which tenant(s) did this content's data come from" is also what makes per-tenant revocation of shared content possible.
- **#251 (status/catalog, sibling design).** #267's drift-integrity and #318's retirement both **depend on #251's canonical logical-name catalog** as the stable key. Sequence #251 first (or co-design the catalog interface). `TableKnowledge` already re-keyed to logical names (#262) — the same anchor.
- **#255 (background robustness, in-flight).** Recipe runs execute on the `recipes` Procrastinate queue and can block on materialization; the #241-A1 option adds a materialization phase — coordinate any new phase with #255's per-tenant locking and janitor so a workspace-transform stage doesn't become a new zombie class. If A2 (drop scope), no collision.
- **#262 (knowledge correctness, merged).** Built on — logical-name keying, import fixes. Don't regress.

## Decisions needed from Brian

1. **Target shape: A (repair-in-place) / B (shared spine, separate models) / C (full unification)?** *Recommendation: **B** — one spine for sharing/provenance/soft-delete, three models keep their lifecycles.*
2. **Unified sharing model + close the public-token tenancy hole?** Give artifacts per-artifact sharing and route every public-token path through the #249 authorizer so revoked tenants lose access. *Recommendation: yes, co-landed with the tenancy window (Phase 2).*
3. **#241 workspace-scope transforms — A1 (run post-view-build) / A2 (drop the scope) / A3 (manual-only + UI)?** The deepest fork; decides whether workspace transforms are a product feature. *Recommendation: **A2** unless there is a real use case, in which case **A1**.*
4. **#318 learning lifecycle — build the real capture→apply→retire loop now, alongside #267?** *Recommendation: yes (Phase 5); retirement keyed on the #251 catalog.*
5. **Provenance — replace `Artifact.conversation_id` CharField with a first-class provenance record?** Needed for drift-detection and correct revocation. *Recommendation: yes (Phase 3).*
6. **Knowledge retrieval — invest in relevance retrieval, or keep the stable-order 6000-char dump?** *Recommendation: keep the dump until a workspace's knowledge volume demonstrably overflows the budget; then Phase 6.*
7. **Golden queries / eval runs — build the models CLAUDE.md advertises, or correct the docs?** They don't exist today. *Recommendation: correct the docs now (Phase 0); build only if there's a product need (a "verified query" library is plausibly valuable but is its own feature).*
8. **`sync_column_notes` tenant→all-workspaces fan-out — accept, or rescope?** Same M2M root as #241; the answer likely follows Decision 3. *Recommendation: revisit under whichever #241 option is chosen.*
