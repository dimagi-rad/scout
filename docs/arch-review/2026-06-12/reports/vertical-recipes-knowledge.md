# Vertical review: Recipes + Knowledge + Learnings (agent-context periphery)

*Reviewer: vertical:recipes-knowledge · 2026-06-12 · HEAD 35e4230 (main)*

Scope: `apps/recipes/`, `apps/knowledge/`, `apps/agents/tools/{learning_tool,recipe_tool}.py`,
the knowledge-retrieval path into `apps/agents/graph/base.py`, the TableKnowledge surface in
`apps/workspaces/api/views.py`, and the frontend slices/pages that consume all of it.

## Headline

**The recipe runner is 100% broken and has been since 2026-03-10.** Every recipe run —
reachable from a live UI button — raises `TypeError` before the agent is even built, returns
HTTP 500, and leaves a `RecipeRun` row stuck in `RUNNING` forever. The breakage is a
3-month-old signature drift between `RecipeRunner` and `build_agent_graph`, invisible to CI
because every runner test mocks `build_agent_graph` (an `AsyncMock` accepts any kwargs) and
the dedicated `tests/test_recipe_runner.py` is four skipped placeholders. Beneath the
signature drift are **three more independent layers of drift** that would each break recipe
runs again the moment the first is fixed.

The knowledge side is healthier: entry CRUD, import/export, table annotations, and the
learning save→inject loop all work on the demo path. But the "learning lifecycle"
(confidence, times_applied, relevance filtering) is largely inert decoration, and
TableKnowledge's free-text schema-qualified keying is a stored-reference time bomb of the
same class the cartography flagged.

## Functional percentage by capability

| Capability | Working? | % functional | Notes |
|---|---|---|---|
| Recipe creation (agent `save_as_recipe` tool) | yes | ~95% | validates variables, matches model |
| Recipe list/detail/edit/soft-delete (API+UI) | yes | ~90% | no role checks; is_shared decorative |
| **Recipe run (the point of the feature)** | **no** | **0%** | TypeError on every invocation since e26cd75 |
| Recipe run history / run detail | partial | ~70% | works, but accumulates orphaned RUNNING rows |
| Recipe sharing (workspace `is_shared`) | no | ~10% | flag stored, never enforced or filtered |
| Recipe public share (`is_public` + token) | partial | ~40% | API + public endpoint live; zero UI to create or view |
| Knowledge entries CRUD (API+UI) | yes | ~95% | shapes match TS types exactly |
| Knowledge import/export (zip+frontmatter) | yes | ~75% | works; multiple uncaught-500 edges, dup-title hazards |
| Table knowledge (annotations via data dictionary) | yes | ~80% | single-tenant happy path solid; keying fragile (below) |
| Learning save (`save_learning` tool) | yes | ~90% | solid validation, dedupe, async ORM |
| Learning injection into prompts | yes | ~85% | top-20 by confidence injected; 60s cache bound staleness |
| Learning lifecycle (confidence/times_applied/relevance) | no | ~10% | admin-only actions; nothing tracks application |
| Golden queries / eval runs (per CLAUDE.md) | n/a | 0% | **do not exist anywhere in the codebase** |

---

## Findings

### F1 — Recipe runs are broken-now: stale `build_agent_graph` call signature (BROKEN-NOW / correctness / verified-by-trace)

**Chain (entry point → consequence):**
1. UI: `frontend/src/pages/RecipesPage/RecipeRunner.tsx` → `recipeSlice.ts:135`
   `api.post(.../recipes/${recipeId}/run/`)` — live Run button.
2. Route: `apps/recipes/urls.py:22` → `RecipeRunView.post` (`apps/recipes/api/views.py:89`).
3. `views.py:107-108`: `runner = RecipeRunner(...)`; `run = runner.execute()`.
4. `apps/recipes/services/runner.py:189`: `self._run = self._create_run_record()` — creates a
   `RecipeRun` with `status=RUNNING` **before** the graph build.
5. `runner.py:191`: `graph = async_to_sync(self._build_graph)()` — *outside* the `try` that
   starts at line 213.
6. `runner.py:115-119`:
   ```python
   self._graph = await build_agent_graph(
       tenant_membership=self._tenant_membership,
       user=self.user,
       checkpointer=None,
   )
   ```
7. `apps/agents/graph/base.py:480-486`:
   `async def build_agent_graph(workspace: Workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)` —
   no `tenant_membership` parameter, no `**kwargs`, `workspace` required.
8. → `TypeError` propagates out of `execute()` to `views.py:109` `except Exception` →
   **HTTP 500** `{"error": "build_agent_graph() got an unexpected keyword argument 'tenant_membership'"}`.
9. The `RecipeRun` created in step 4 is never updated → **permanently `RUNNING`** in the runs
   list; no janitor touches `RecipeRun` (grep of `apps/workspaces/tasks.py` and the recipes app: zero reconciliation).

**Mechanical verification:** `inspect.signature(build_agent_graph).bind(tenant_membership=None, user=None, checkpointer=None)`
→ `TypeError: missing a required argument: 'workspace'` (run against HEAD).

**Archaeology:** the runner call shape dates to `32e9aac` (2026-02-20); `build_agent_graph`
gained the `workspace`-first signature in `e26cd75` "Phase 9 — multi-tenant workspaces"
(2026-03-10) without migrating this caller. The other three callers (`apps/chat/views.py:167,179`,
`apps/workspaces/tasks.py:858-864`) were migrated/written correctly; recipes was simply
forgotten. This is the exact "recipes↔graph signature" drift v1 flagged — still unfixed at HEAD.

Reachable via: Recipes page → recipe detail → Run button (also raw API). Complexity: accidental.

### F2 — Three more drift layers beneath F1: recipe runs would still be broken after the obvious fix (LATENT / correctness / strong-inference)

Fixing the kwarg alone does not make recipes work:

1. **No MCP tools.** Runner never passes `mcp_tools` → `base.py:500` `_build_tools(workspace, user, [])`
   → the recipe agent has only `save_learning`/artifact/recipe tools and **no `query`, no
   `list_tables`** — it cannot touch data. Compare `apps/workspaces/tasks.py:855-864`, which
   loads `get_mcp_tools()` + oauth tokens; runner.py imports neither.
2. **Stale state keys.** `runner.py:215-224 / 302-311` builds `initial_state` with
   `tenant_id`, `tenant_name`, `tenant_membership_id`, `user_role` — but `AgentState`
   (`apps/agents/graph/state.py:80-148`) is `{messages, workspace_id, user_id, user_role, thread_id}`.
   `workspace_id` — the key injected into every MCP tool call (`base.py:504-508`) — is never
   set, so every MCP tool would receive `workspace_id=""` and fail context resolution.
3. **Sync invoke of an async graph.** `execute()` (the only production path, `views.py:108`)
   calls `graph.invoke(...)` (`runner.py:226`) on a compiled graph whose nodes are
   `async def` (`base.py:538` `agent_node`); LangGraph cannot run async nodes from the sync
   entry point. The async twin `execute_async()` (`runner.py:257`) has **zero production
   callers** — dead code.

Also note the design hazard for whoever fixes this: `execute()` runs an entire multi-turn
agent loop synchronously inside one DRF request — no deferral to the worker, no timeout
budget, no streaming. The fix should probably be a rewrite against the chat/resume invocation
pattern, not a patch.

### F3 — Test architecture hides the entire recipes↔agent seam (DEBT / velocity / verified-by-trace)

- Every `RecipeRunner` test patches the seam:
  `tests/test_recipes.py:584,599,624,...` `@patch("apps.recipes.services.runner.build_agent_graph")` —
  `unittest.mock` auto-substitutes an `AsyncMock` which accepts **any** kwargs, so the F1
  TypeError is structurally invisible. All 44 tests pass at HEAD (run during this review).
- `tests/test_recipe_runner.py` is 27 lines: four `@pytest.mark.skip(reason="TODO: implement")`
  placeholders, including `test_recipe_execution`.
- There is no HTTP-level test of `POST .../run/`; no QA scenario for recipes
  (`tests/qa/` contains only `data-dictionary-scenario.md`).
- Same pattern on knowledge: `tests/test_knowledge_views.py` is a single test;
  import/export endpoints have zero tests;
  `tests/test_knowledge_retriever.py:389` `test_question_based_table_filtering` asserts
  `"orders" in result` — trivially true because the retriever injects *everything*; the test
  documents a filtering feature that does not exist and can never fail.

This is a textbook instance of the project-memory lesson "mocking the resolver hid the bug":
a feature dead for 3 months with a green suite.

### F4 — Recipe privacy flags are decorative; role checks absent across recipes & knowledge (DEBT / security / verified-by-trace)

- `Recipe.is_shared` help_text and the `save_as_recipe` tool docstring
  (`apps/agents/tools/recipe_tool.py:86-87`) promise "Default is False (only the creator can
  see it)". But `RecipeListView.get` (`apps/recipes/api/views.py:38`) returns
  `Recipe.objects.filter(workspace=workspace)` — **no `created_by` or `is_shared` filter**, and
  `RecipeDetailView`/`RecipeRunListView` likewise. Every workspace member sees, edits, runs
  and deletes every "private" recipe. The agent is actively telling users a false privacy claim.
- No role enforcement anywhere in recipes or knowledge views: `resolve_workspace_drf`
  (`apps/workspaces/workspace_resolver.py:12-32`) checks membership only. A `READ`-role member
  can delete recipes (`views.py:76-81`), delete knowledge entries and learnings
  (`apps/knowledge/api/views.py:212-224`), import/overwrite the knowledge base, and (once F1
  is fixed) run recipes that consume LLM spend. Contrast `TableDetailView.put`
  (`apps/workspaces/api/views.py:500-504`) and `RefreshSchemaView` (`views.py:330`), which do
  check roles — enforcement is inconsistent within the same file family. (Confirms the
  "roles ~unenforced" seed for this vertical.)

### F5 — Recipe share-surface drift: live public API, dead frontend (DEBT / security / verified-by-trace)

- `RecipeRunUpdateSerializer` (`apps/recipes/api/serializers.py:108-113`) still accepts
  `is_public`; `RecipeRun.save()` (`models.py:387-392`) mints a share token; public endpoint
  `GET /api/recipes/runs/shared/<token>/` is wired (`config/urls.py:102-104`,
  `PublicRecipeRunView` with `AllowAny`).
- The share-creation UI was removed 2026-06-04: `RecipeDetail.tsx`/`RecipeRunDetail.tsx`
  retain `is_public` only in type signatures (lines 25, 80), no rendered toggle.
- `PublicRecipePage.tsx` and `PublicRecipeRunPage.tsx` exist but are **not in
  `frontend/src/router.tsx`** — unreachable dead components. A token-holder gets raw JSON
  (full prompts + step results, `PublicRecipeRunSerializer`), with no page to render it.
- Edge: a public run remains publicly accessible after its parent recipe is soft-deleted
  (`PublicRecipeRunView` queries `RecipeRun` directly, no `recipe.is_deleted` check).

So the public exposure path can only be *created* via raw API today, but anything made public
historically (or by API callers) stays exposed forever with no UI to audit or revoke it.

### F6 — Dead code cluster across the vertical (DEBT / velocity / verified-by-trace)

- `RecipeStep` model + `RecipeStepAdmin` + ~150 lines of tests: replaced by the single
  `Recipe.prompt` field in `c2a75f8`; zero non-admin/non-test references. Test fixtures still
  create `RecipeStep` rows that nothing reads.
- `RecipeRunner.execute_async` (`runner.py:257-335`): zero callers.
- `apps/knowledge/api/views.py:148-150`: `elif item_type == "learning"` branch in `post()` is
  unreachable — learnings are rejected with 400 at lines 121-127 above.
- `Workspace.data_dictionary` legacy JSONField has **no production writers** (only
  `purge_synced_data` nulls it). Consequences: the fallback in
  `TableDetailView._get_table_data` (`workspaces/api/views.py:425-427`) is dead, and the
  `save_learning` table-validation block (`learning_tool.py:167-179`) always no-ops because
  `known_tables` is always empty — the "validate tables exist" behavior is fictional.
- Stale docstring: `apps/agents/memory/checkpointer.py:108` example still calls
  `build_agent_graph(project, ...)` — pre-rename residue, and coincidentally the same
  signature-drift family as F1.
- Docs drift: `CLAUDE.md` says the knowledge app contains "golden queries, eval runs" —
  grep for golden/eval-run across apps, mcp_server, frontend returns nothing. The
  `KnowledgeEntry` docstring confirms those concepts were collapsed into tagged entries.
  (Also: the cartography map calls the models `TableMetadata`/`Learning`; actual names are
  `TableKnowledge`/`AgentLearning`.)

### F7 — Learning lifecycle is inert; retrieval has no relevance dimension (DEBT / correctness / verified-by-trace)

The model promises a feedback loop it doesn't have:

- `confidence_score` help_text: "Increases when the learning is confirmed useful, decreases
  if contradicted." Actual writers of `increase_confidence`/`decrease_confidence`: **Django
  admin bulk actions only** (`apps/knowledge/admin.py:162-173`). No agent or API path adjusts
  confidence from outcomes.
- `times_applied` is incremented in exactly one place: when the agent re-saves a learning with
  a byte-identical (`__iexact`) description (`learning_tool.py:182-192`) — vanishingly
  unlikely for LLM-generated prose. Learnings are injected into *every* prompt yet
  "application" is never counted; the retriever then renders "*(applied N times)*"
  (`retriever.py:135-138`) — a confident-looking number that is essentially always 0.
- `KnowledgeRetriever.retrieve(user_question="")` (`retriever.py:33`): the parameter is
  **never read** and the single call site passes nothing (`base.py:731`). There is no
  relevance filtering of any kind: all entries + all table knowledge + top-20 learnings are
  injected wholesale into every system prompt. `applies_to_tables` ("for relevance
  filtering", per the tool docstring) is display-only.
- Consequence at scale: knowledge context is unbounded (entries and table knowledge have no
  cap), so a workspace that imports a large knowledge zip linearly inflates every prompt —
  cost/latency, and eventually context pressure. Essential complexity for the feature is a
  relevance layer; what exists is a formatter.

### F8 — TableKnowledge keyed by physical schema-qualified name: orphaning + cross-schema prompt contamination (LATENT / correctness / strong-inference)

`TableKnowledge.table_name` stores `f"{schema_name}.{table_name}"` where `schema_name` is the
**physical tenant schema** (`workspaces/api/views.py:289-290` write-side via
`TableDetailView.put` at 524-527; read-side `_get_annotation` 216-222).

- **Refresh orphaning:** `SchemaManager.create_refresh_schema` (`schema_manager.py:176`)
  generates `{base}_r{uuid8}` — a *new* schema name. After a legacy `/refresh/`
  (route still wired), every annotation keyed to the old name stops resolving in the data
  dictionary. (Reachability is whatever the legacy refresh path's is — itself flagged S1
  elsewhere; normal materialization reuses a stable name via `provision()`, so the demo path
  is safe.)
- **Asymmetric consumers:** the dictionary looks annotations up by current qualified name and
  silently shows nothing for orphans, but `KnowledgeRetriever._format_table_knowledge`
  (`retriever.py:68-113`) injects **all** rows with their stored names as headings. Orphaned
  rows keep feeding the agent `### old_schema_r1234.users` — schema names that no longer
  exist. This is raw material for the contradictory-schema panic-loop class (#190).
- **Multi-tenant mismatch:** `DataDictionaryView` annotates against
  `workspace.tenant` = `tenants.first()` (unordered M2M, `workspaces/models.py:144-146`) —
  i.e., first-tenant physical schema — while the agent in a multi-tenant workspace queries the
  workspace *view schema*. Annotations are therefore keyed to a schema the agent is not
  supposed to query, and which tenant gets annotated is nondeterministic.

This is precisely the "stored free-text references to schema objects" seam from the
cartography map, instantiated in this vertical.

### F9 — Knowledge import/export edge failures (LATENT / correctness / verified-by-trace on code paths)

`KnowledgeImportView.post` (`apps/knowledge/api/views.py:271-318`) catches only
`zipfile.BadZipFile`. Uncaught 500s, each reachable by an authenticated member uploading a
crafted/sloppy zip:

- `parse_frontmatter` (`utils.py:28`): `text.index("---", 3)` → `ValueError` on a file that
  opens frontmatter and never closes it.
- `yaml.safe_load` → `yaml.YAMLError` on malformed frontmatter (utils.py:32).
- `zf.read(name).decode("utf-8")` → `UnicodeDecodeError` (views.py:290).
- `update_or_create(workspace=..., title=...)` (views.py:295) → `MultipleObjectsReturned`:
  `KnowledgeEntry` has **no uniqueness on (workspace, title)** and the create endpoint happily
  makes duplicates, so importing into a workspace with duplicate-titled entries 500s halfway
  through a non-atomic loop (partial import, no transaction).
- Export side (`views.py:243-251`): duplicate titles produce duplicate filenames in the zip;
  on re-import the last one wins — silent entry loss on an export→import round trip.

Low impact (authenticated, self-inflicted), but the import loop should be transactional and
the parser total.

### F10 — In-memory pagination of the knowledge list (DEBT / cost-perf / verified-by-trace)

`KnowledgeListCreateView.get` (`views.py:74-97`) loads **every** entry and learning in the
workspace, serializes all of them, concatenates, sorts in Python, then slices the requested
page. `page_size` merely changes the slice. Fine at dozens of rows; quadratic-feeling at
thousands (every page load re-serializes the world). Accidental complexity — two models
behind one endpoint chose the easy merge.

### F11 — Cosmetics (COSMETIC)

- Nested duplicate heading: `base.py:733` wraps retriever output in `## Knowledge Base`, and
  the retriever's first section is itself `## Knowledge Base` (`retriever.py:58`) — the agent
  sees the heading twice at the same level.
- `runner._extract_response_content` (`runner.py:164`) does `str(msg.content)`; Anthropic
  content can be a list of blocks → stored run "response" becomes a Python repr of a list.
  Moot while F1 stands.
- `RecipeRunView` touches the TTL schema *after* the run completes (`views.py:113-122`) — for
  a long synchronous run the touch lands late; harmless, just odd ordering. Multi-tenant: only
  the first tenant's schema is touched.

---

## What's actually fine

- **Knowledge entries CRUD end-to-end**: API response shapes (incl. the
  `results`/`pagination` envelope and the `type` discriminator emitted by both serializers)
  match `frontend/src/store/knowledgeSlice.ts` exactly; UI list/form/detail wired; learnings
  correctly blocked from manual creation server-side with a test covering it.
- **`save_learning` tool**: input validation, iexact dedupe, fully-async ORM, graceful error
  envelope back to the LLM; learnings genuinely flow into subsequent prompts via the
  retriever, and the 60s system-prompt cache (`base.py:128`) keeps staleness bounded.
- **`save_as_recipe` tool**: matches the current `Recipe` model (`prompt` field — the tool was
  migrated when `RecipeStep` was collapsed), validates variable specs and undefined
  `{{placeholder}}` references; creation works in real DB tests.
- **Workspace scoping**: every recipes/knowledge endpoint goes through
  `resolve_workspace_drf` (membership required); DRF default `IsAuthenticated` covers views
  that omit `permission_classes`; the public run serializer excludes share tokens and the
  public view requires `is_public=True`.
- **Soft delete**: `RecipeSoftDeleteManager` consistently hides deleted recipes from all
  list/detail paths; `all_objects` reserved for admin.
- **The other `build_agent_graph` callers** (chat view, resume task) use the current
  signature, load MCP tools, and pass real `AgentState` keys — recipes is the *only*
  un-migrated consumer.

## Coverage log

**Deep-read:** `apps/knowledge/{models,urls,utils}.py`, `apps/knowledge/api/{views,serializers}.py`,
`apps/knowledge/services/retriever.py`, `apps/recipes/{models,urls}.py`,
`apps/recipes/api/{views,serializers}.py`, `apps/recipes/services/runner.py`,
`apps/agents/tools/learning_tool.py`, `apps/agents/tools/recipe_tool.py`,
`apps/agents/graph/state.py`, `apps/agents/graph/base.py` (lines 1–80, 480–540, 640–794),
`apps/workspaces/api/views.py` (lines 180–541), `apps/workspaces/workspace_resolver.py`,
`apps/agents/memory/checkpointer.py` (85–130), `frontend/src/store/knowledgeSlice.ts`,
`frontend/src/router.tsx`, `tests/test_recipe_runner.py`, `tests/test_knowledge_views.py`,
`tests/test_recipes.py` (runner + tool sections, 560–870).

**Skimmed (greps + targeted reads):** `apps/knowledge/admin.py`, `apps/recipes/admin.py`,
`apps/workspaces/services/schema_manager.py` (name generation only),
`apps/workspaces/tasks.py` (resume call site only), `apps/workspaces/models.py` (tenant
properties), `config/urls.py`, `config/settings/base.py` (REST_FRAMEWORK),
`frontend/src/store/recipeSlice.ts`, `frontend/src/pages/RecipesPage/*` (grep-level),
`frontend/src/pages/KnowledgePage/KnowledgeForm.tsx` (grep-level),
`frontend/src/store/dictionarySlice.ts` (qualified-name construction only),
`tests/test_knowledge_retriever.py` (class list + filtering test).

**Not examined:** full `RecipeRunner.tsx`/`RecipeDetail.tsx`/`RecipeRunDetail.tsx` rendering
logic; `PublicRecipePage.tsx`/`PublicRecipeRunPage.tsx`/`PublicThreadPage.tsx` internals and
how (or whether) they are served at all; `frontend/src/pages/DataDictionaryPage` annotation
UI; `apps/agents/tools/artifact_tool.py` internals; chat streaming path
(`apps/chat/stream.py`, `thread_views.py`); MCP server internals; `tests/test_recipe_soft_delete.py`;
`tests/test_models.py` knowledge sections; conftest fixtures; knowledge/recipes admin
behavior under multi-workspace; production data (cannot count orphaned RUNNING RecipeRun rows
or historical `is_public` runs — recommend a prod query for both); Langfuse tracing of recipe
runs; migrations history of either app.
