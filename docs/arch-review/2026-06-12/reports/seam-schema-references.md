# Seam review: materialization ↔ stored schema references

*Reviewer: seam:schema-references. Mandate: own the contract between materialization
(schemas, views, tables that get created, renamed, and destroyed) and everything that
stores schema references as free text (artifact SQL, knowledge content, learnings,
recipe prompts). Core question: what detects drift?*

**Answer to the mandate question: nothing detects drift.** Proven below, not assumed.
There is no validation at write time (beyond one advisory check against a dead legacy
field), no reconciliation job, no reference migration accompanying any rename event,
and no read-time check other than the raw runtime error a user sees when an artifact
query fails. For knowledge/learnings the drift never surfaces at all — stale names are
injected into every system prompt indefinitely.

Beyond the drift question, tracing the replay paths found that **two of the three
replay surfaces are broken outright today**: artifact live queries in multi-tenant
workspaces resolve to the wrong schema, and the recipe-run endpoint crashes on a
drifted function signature before the prompt ever reaches the agent.

---

## The contract, as implied by the code

The creation side (chat agent) and the replay sides (artifact render, recipe run,
prompt assembly) implicitly agree on:

1. **Name space**: table/view names the agent saw at creation time still resolve at
   replay time.
2. **Resolution context**: replay executes in the *same* schema context the agent
   queried in (for multi-tenant workspaces: the `ws_*` view schema with
   `{prefix}__{table}` views; for single-tenant: the `t_*`-style tenant schema with
   plain names).
3. **Liveness**: the knowledge injected into the prompt describes tables that exist.

The stores that hold these free-text references:

| Store | Field(s) | Written by | Replayed by |
|---|---|---|---|
| `Artifact` | `source_queries` (JSON SQL), `code` | `create_artifact`/`update_artifact` tools (`apps/agents/tools/artifact_tool.py:188,296`) | `ArtifactQueryDataView` (`apps/artifacts/views.py:764`), sandbox fetch (`views.py:254`) |
| `Recipe` | `prompt` template | recipe tool / API | `RecipeRunner` (`apps/recipes/services/runner.py`) |
| `TableKnowledge` | `table_name` (**schema-qualified**), `related_tables`, `column_notes` | dictionary annotate PUT (`apps/workspaces/api/views.py:524`) | `KnowledgeRetriever` → system prompt |
| `AgentLearning` | `applies_to_tables`, `original_sql`, `corrected_sql` | `save_learning` tool (`apps/agents/tools/learning_tool.py:210`) | `KnowledgeRetriever` → system prompt |
| `KnowledgeEntry` | free markdown `content` | knowledge API + zip import | `KnowledgeRetriever` → system prompt |
| LangGraph checkpoints | serialized messages with SQL/table names | checkpointer | thread resume (not deep-traced here) |

The renaming side — every event that changes the names these stores reference:

- **View prefix derivation** (`schema_manager.py:_view_prefix`, line 219): prefix =
  sanitized `tenant.canonical_name` if ≤32 chars, else `head23 + "_" + sha256(external_id)[:8]`.
  Changes whenever `canonical_name` changes, and changed wholesale in PR #228 (`2aaf4fb`).
- **Tenant-count transitions** (1↔2 tenants): the whole naming regime flips between
  plain `{table}` (tenant schema) and `{prefix}__{table}` (view schema);
  `remove_workspace_tenant` tears the view schema down.
- **Refresh path** (`/api/workspaces/<id>/refresh/` → `create_refresh_schema`,
  `schema_manager.py:169`): every refresh creates a **new** physical schema
  `{base}_r{uuid8}`, activates it, and tears down the old one (`tasks.py:126-199`).
  The active schema *name* changes on every refresh.
- **Historical**: `965a407` replaced UNION-ALL merged views (plain names) with
  namespaced views; `2aaf4fb` (PR #228) re-derived prefixes for long-named tenants.
- **dbt/transformation model renames** (not traced in depth — see coverage log).

None of these events is accompanied by any migration, rewrite, invalidation, or even
flagging of the stored references above. Verified by grep: `source_queries` is touched
only by the two artifact tools, the replay/serialize views, and admin display — no
other consumer exists in `apps/` or `mcp_server/`.

---

## Finding 1 — Artifact live queries in multi-tenant workspaces execute against the wrong schema (BROKEN-NOW, correctness, verified-by-trace)

The creation context and the replay context disagree about which schema an artifact's
SQL runs in.

**Creation side** (multi-tenant workspace):
1. MCP `query`/`list_tables` resolve via `_resolve_mcp_context` → `load_workspace_context`
   (`mcp_server/server.py:71-75`).
2. For 2+ tenants, `load_workspace_context` returns a context whose
   `schema_name = WorkspaceViewSchema.schema_name` (`mcp_server/context.py:113-139`) —
   the `ws_<hex16>` schema containing only `{prefix}__{table}` views
   (`schema_manager.py:334,367-375`).
3. The system prompt explicitly instructs the agent: *"Tables are namespaced views
   prefixed with the tenant name using double underscore: `{tenant_name}__{table_name}`"*
   (`apps/agents/graph/base.py:299-303`), and lists those prefixed names
   (`base.py:345-393`).
4. The agent stores exactly that SQL in `Artifact.source_queries`
   (`artifact_tool.py:188-199`); the tool docstring promises "executed at render time
   against the workspace database" (`artifact_tool.py:127-130`).

**Replay side**:
5. Sandbox iframe fetches `/api/workspaces/<id>/artifacts/<id>/query-data/`
   (`apps/artifacts/views.py:254`).
6. `ArtifactQueryDataView.get`: `tenant = await artifact.workspace.tenants.afirst()`
   (`views.py:795`) then `ctx = await load_tenant_context(tenant.external_id)`
   (`views.py:800`) — **always a single tenant's schema**, never
   `load_workspace_context`. (`tenants` is an unordered M2M, so *which* tenant is
   first is also unspecified.)
7. `execute_query` sets `search_path` to that tenant schema
   (`mcp_server/services/query.py:47`), where no `{prefix}__{table}` relation exists.

**Consequence**: every live-data artifact created in a multi-tenant workspace fails at
render time with per-query `relation ... does not exist` errors (surfaced as "Data
Fetch Error" / per-query error chips in the sandbox, `views.py:824-831`). If the agent
ever wrote unprefixed SQL instead, the artifact would silently render **one tenant's
data** as if it were the whole workspace — arguably worse. The same view also touches
only the `TenantSchema` TTL (`views.py:810-812`), not the view schema's, so artifact
usage doesn't keep the view schema alive.

**Reachability**: live. Multi-tenant workspaces are a flagship path (the entire
2026-06-10 incident cluster, PRs #227–#230, is about keeping their view schemas
working), and the artifact prompt mandates `source_queries` for every data-driven
artifact (`apps/agents/prompts/artifact_prompt.py:100-110`).

**Complexity**: accidental — `load_workspace_context` already exists and does the
correct routing; the artifact view predates it and was never migrated. The workspace
is already in hand at `views.py:795`; the fix is to call the workspace-level resolver.

## Finding 2 — Recipe execution crashes on a drifted `build_agent_graph` signature; runs stuck RUNNING (BROKEN-NOW, correctness, verified-by-trace)

Recipes are the purest "stored prompt replayed later" surface — and the replay machinery
does not run at all.

**Chain**:
1. `POST /api/workspaces/<id>/recipes/<recipe_id>/run/` → `RecipeRunView.post`
   (`apps/recipes/api/views.py:89-108`) → `RecipeRunner(recipe, variable_values, user).execute()`.
2. `execute()` creates the `RecipeRun` row with `status=RUNNING`
   (`runner.py:188`, `_create_run_record` at 123-143), **then** calls
   `graph = async_to_sync(self._build_graph)()` (`runner.py:191`) — outside its
   try/except.
3. `_build_graph` calls
   `build_agent_graph(tenant_membership=self._tenant_membership, user=self.user, checkpointer=None)`
   (`runner.py:115-119`).
4. The actual signature is
   `async def build_agent_graph(workspace: Workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)`
   (`apps/agents/graph/base.py:480-486`). There is no `tenant_membership` parameter and
   `workspace` is required → `TypeError` on every call.
5. The view's broad `except Exception` (`api/views.py:109-111`) converts this to a 500;
   the `RecipeRun` row is never updated and remains `RUNNING` forever.

Even if the signature matched, the runner's `initial_state`
(`runner.py:215-224, 302-311`) populates `tenant_id` / `tenant_name` /
`tenant_membership_id` / `user_role` — but `AgentState` has none of the first three
fields (`apps/agents/graph/state.py:80-148`), and the MCP injection map reads
`workspace_id` / `user_id` / `thread_id` from state (`base.py:503-508`). With no
`workspace_id`, `_resolve_mcp_context` raises `"workspace_id is required"`
(`server.py:73-74`) — every data tool would fail. The runner has drifted across at
least two graph-contract revisions (tenant-flow `cc8f186` → workspace flow `e26cd75`).

**Why no one noticed**: the tests inject a fake graph —
`RecipeRunner(recipe, values, user, graph=mock_graph)` (`tests/test_recipes.py:696`) —
which bypasses `_build_graph` entirely. The mocked seam is exactly the broken seam.

**Reachability**: live route (`/api/workspaces/<id>/recipes/<id>/run/`), Recipes page in
the frontend router. Severity is "feature dead + orphaned RUNNING rows", not data loss.

**Complexity**: accidental (missed migration of a caller).

Note the second-order effect for this seam: because replay is dead, recipe-vs-rename
drift (a recipe prompt naming `{old_prefix}__visits`) is currently *masked*. Fixing the
runner without addressing Finding 4 will resurrect recipes that silently reference
renamed views — the agent will see tool errors and may or may not recover.

## Finding 3 — Nothing validates or reconciles stored references; the one nominal check runs against a dead field (LATENT, correctness, verified-by-trace for the absence)

The mandate's central question, answered mechanism by mechanism:

- **Artifacts**: `create_artifact` validates type/title/code only
  (`artifact_tool.py:153-185`); `source_queries` is persisted verbatim from the LLM.
  Nothing checks the SQL parses, that referenced tables exist, or even that the queries
  are the ones the agent actually ran. The query pipeline *already computes*
  `tables_accessed` per query (`mcp_server/services/query.py:117,144`) — a cheap
  validation hook exists and is unused. No job, command, or view re-checks
  `source_queries` after any schema/view rebuild. Drift surfaces only as a render-time
  error to the end user.
- **Learnings**: `save_learning` "validates" `applies_to_tables` against
  `workspace.data_dictionary` (`learning_tool.py:167-179`) — but (a) the check is
  advisory (logs a warning, comment: "Don't fail"), and (b) `Workspace.data_dictionary`
  is a legacy field with **no remaining writer**: the only non-test code that assigns
  it is `purge_synced_data`, which sets it to `None`
  (`apps/workspaces/management/commands/purge_synced_data.py:61`); the dictionary API
  reads it only as a fallback (`apps/workspaces/api/views.py:425-427`). For any
  workspace whose field is `None`, `known_tables` is empty and the check is skipped
  entirely (`learning_tool.py:168-171`). So the single validation that exists compares
  against a catalog that is empty or frozen in time.
- **Knowledge injection**: `KnowledgeRetriever` injects *all* `KnowledgeEntry` rows,
  *all* `TableKnowledge` rows, and the top-20 `AgentLearning` rows by confidence into
  every system prompt (`apps/knowledge/services/retriever.py:51-140`) with zero
  liveness check against the current catalog. A learning about a table that no longer
  exists is repeated to the agent forever.
- **Learning lifecycle**: `AgentLearning.confidence_score`'s docstring claims it
  "Increases when the learning is confirmed useful, decreases if contradicted"
  (`apps/knowledge/models.py:169`). The only callers of
  `increase_confidence`/`decrease_confidence` are manual Django-admin actions
  (`apps/knowledge/admin.py:162-173`); `times_applied` is incremented only when the
  agent re-saves a duplicate description (`learning_tool.py:190-192`). There is no
  automatic feedback loop, so a drifted learning never retires (comment-vs-code
  mismatch — flagged per evidence standards).
- **Reconciliation**: no janitor, cron task, or management command inspects any of
  these stores (verified by grep across `apps/` and `mcp_server/`; the only
  `source_queries` consumers are listed above; cron tasks in `tasks.py` are
  `expire_inactive_schemas` and `expire_stale_thread_jobs`, neither reference-aware).

**Complexity**: mixed. Free-text SQL/table references are essential to the product
(LLM-generated SQL is the product). The absence of *any* read- or write-time liveness
check, while the validator already extracts `tables_accessed`, is accidental.

## Finding 4 — Rename events silently invalidate stored references; PR #228 was such an event (LATENT, correctness, strong-inference)

Blast-radius map of each rename mechanism, against the stores:

1. **PR #228 / `2aaf4fb` (2026-06-10)**: before, the view prefix was the full
   sanitized `canonical_name` (unbounded; PostgreSQL silently truncated composed names
   at 63 bytes). After, any tenant whose sanitized name exceeds 32 chars gets
   `head23_digest8`. Consequence classes:
   - Tenants with sanitized names of 33–~50 chars whose composed view names fit in 63
     bytes had *working, untruncated* views before the fix; the fix **renamed every
     view of every such tenant**. The commit message itself names a production tenant
     in this class ("Kangaroo Mother Care- Preterm Infants Parents Network (PIPN)" →
     51-char prefix).
   - Tenants whose names truncated at 63 bytes without colliding had working
     (truncated) names that the agent saw via `information_schema` and could have
     stored; those also all changed.
   - No migration touched `Artifact.source_queries`, `AgentLearning.*`,
     `Recipe.prompt`, `TableKnowledge`, or checkpoints. Confidence is
     strong-inference rather than verified only because I cannot inspect prod rows to
     confirm artifacts referencing old prefixes exist; the mechanism and the absence
     of migration are verified.
2. **`canonical_name` changes upstream**: `_view_prefix` is a pure function of
   `canonical_name` (+`external_id` for long names) (`schema_manager.py:236-241`). A
   provider-side org rename changes every view name at the next rebuild — and rebuilds
   are routine (`build_view_schema` is called on rematerialization and sibling-rebuild
   paths, per PRs #227–#230). Nothing pins or versions the prefix.
3. **Tenant-count transitions**: adding a second tenant flips the agent's world from
   plain names to prefixed views; dropping to one tenant tears the view schema down
   (`context.py:88-94` documents the invariant). Knowledge/learnings/recipes created
   in one regime are misinformation in the other. Artifacts created single-tenant
   keep working only by the accident of Finding 1's bug (replay pins the first tenant).
4. **Every `/refresh/`**: see Finding 5.
5. **Historical `965a407`** (UNION-ALL → namespaced views): same class, older;
   anything stored against the merged plain-name views died then.

**Detection for all five: none** (Finding 3). Severity is muted today only because
Finding 1 already breaks multi-tenant artifact replay and Finding 2 breaks recipe
replay — the renamed references sit behind already-broken replay paths. Fixing those
two without a reference audit will surface this one as "my dashboards all show query
errors" tickets.

## Finding 5 — `TableKnowledge` keys embed the physical schema name; every refresh orphans every annotation (LATENT→DEBT, correctness, verified mechanism / strong-inference impact)

`TableKnowledge` rows are keyed by `qualified_name` = `"{schema_name}.{table_name}"`
(`apps/workspaces/api/views.py:524-527`, key built from the URL of
`PUT /data-dictionary/tables/<qualified_name>/`, which the dictionary UI constructs
from the *current* `tenant_schema.schema_name`, `views.py:413-427`).

But the refresh path renames the physical schema on every invocation:
`create_refresh_schema` mints `{base}_r{uuid4.hex[:8]}` (`schema_manager.py:169-181`),
`refresh_tenant_schema` activates it and schedules teardown of the old schema
(`tasks.py:126-199`). After one refresh:

- Dictionary lookups (`views.py:217-221`, `TableKnowledge.objects.get(workspace=...,
  table_name=table_name)` with the *new* qualified name) miss every existing
  annotation → column notes, quality notes, ownership silently vanish from the UI.
- The retriever still injects the old rows into the prompt as
  `### t_oldschema.raw_visits` (`retriever.py:77-81`) — schema-qualified names the
  SQL validator will *reject* if the agent copies them
  (`mcp_server/services/sql_validator.py:270-287` allows only the current schema +
  `public`), and the dictionary UI can never edit again.
- Re-annotating creates a parallel row under the new schema name; rows accumulate, one
  generation per refresh, all but the newest dead.

Single-tenant artifact SQL, by contrast, survives refresh because it is unqualified
and resolved via `search_path` (`context.py:159`, `query.py:47`) — which makes the
schema-qualified `TableKnowledge` key the odd one out, and makes the prompt's
qualified headers actively harmful (they teach the agent a naming style that breaks on
the next refresh).

**Reachability**: `/api/workspaces/<id>/refresh/` is wired (`workspaces/api/views.py:362`).
Caveat: v1 run A flagged the refresh task itself as broken (loads into the old schema
then destroys it — not re-verified here); if refresh never completes successfully in
prod, the orphaning is correspondingly rarer. The key-design flaw is verified either way.

**Complexity**: accidental — keying by plain table name (or by a stable logical
identifier) loses nothing; the workspace FK already scopes the row.

## Finding 6 — Multi-tenant namespace hint misdescribes hashed prefixes (COSMETIC, correctness, verified-by-trace)

`_MULTI_TENANT_NAMESPACE_HINT` tells the agent tables are *"prefixed with the tenant
name"* (`base.py:299-303`). For any tenant whose sanitized canonical name exceeds 32
chars, the prefix is `head23_digest8` (`schema_manager.py:236-241`) — not the tenant
name. The agent does receive real names from `list_tables`/the prompt table list, so
this mostly self-corrects, but it is exactly the prompt↔reality drift class that
produced #190/`93504d5`, recurring one layer up. Worth fixing when touching the prompt.

---

## What detects drift today — the honest inventory

| Surface | Detection | User-visible symptom of drift |
|---|---|---|
| Artifact `source_queries` | none until execution | per-query error chip / "Data Fetch Error" in sandbox |
| Artifact `code` (data keys) | none | empty charts (`merged[q.name]` never set when query errors, sandbox `mergeQueryResults`) |
| Recipe `prompt` | none (replay currently crashes anyway) | n/a today; post-fix: agent tool errors mid-run |
| `TableKnowledge` | none | annotations silently disappear from dictionary UI; stale prompt context |
| `AgentLearning` | advisory log-only check vs. dead legacy field | none — stale learnings injected forever |
| `KnowledgeEntry` | none (free markdown) | none |
| Checkpoints | none (LLM may self-correct on resume) | tool errors on resumed threads |

## Recommendations (in seam order, not effort order)

1. Route artifact replay through `load_workspace_context(artifact.workspace_id)`
   (one-line conceptual fix + TTL touch of the right schema row) — fixes Finding 1.
2. Fix `RecipeRunner._build_graph`/`initial_state` to the workspace contract, and add
   one integration test that builds the *real* graph (the `graph=` injection seam is
   what hid this for ~3 months) — fixes Finding 2.
3. Add a write-time liveness check: on `create_artifact`/`save_learning`, resolve the
   referenced tables against `information_schema` of the *current* context (the
   validator already extracts `tables_accessed`); warn the agent in the tool response
   rather than failing.
4. Add a read-time staleness pass: when assembling the prompt, drop or annotate
   knowledge/learnings whose tables don't exist in the current catalog ("table no
   longer exists — may be renamed").
5. Re-key `TableKnowledge` by plain table name; one data migration to strip schema
   prefixes from existing keys.
6. When `build_view_schema` produces a different prefix for a tenant than the previous
   build (it can compare against the views it just dropped), log it loudly and/or
   enqueue a reference audit — that is the single chokepoint through which every
   rename event flows.

## What's fine

- **Unqualified SQL + `search_path` resolution** (`context.py:142-163`,
  `query.py:44-49`) makes single-tenant artifact SQL rename-resistant across schema
  re-provisioning and even the `_r{hex}` refresh renames — good essential design; the
  failures above are exactly the places that deviate from it.
- **`_view_prefix` is now deterministic and rebuild-stable** (digest of `external_id`;
  unit-tested in `2aaf4fb`) — going forward, rebuilds alone no longer rename views.
- **`row_count` → `materialized_row_count` rename was migrated consistently**: MCP
  metadata emits `materialized_row_count`/`row_count_verified`
  (`mcp_server/services/metadata.py:90-91`), frontend consumes those keys
  (`ToolOutput.tsx:254-291`); the `row_count` in `ArtifactPanel.tsx` is the *query
  result* row count, a different field, correctly named. (Skim-level confidence.)
- **Chat path context injection is correct**: `chat/views.py:211` puts
  `workspace_id` into state; the injection map (`base.py:503-508`) matches `AgentState`.
- **Knowledge zip import is conservative**: imports only
  `KnowledgeEntry(title/content/tags)` (`knowledge/api/views.py:295-305`) — it cannot
  fabricate structured table references (the markdown can still carry stale names, but
  that's inherent).
- **`_parse_db_url` defence-in-depth** re-validates schema names before embedding in
  connection options (`context.py:145-149`).

## Coverage log

**Deep-read (line-by-line):**
`apps/artifacts/models.py`, `apps/artifacts/views.py` (all 989 lines incl. sandbox JS),
`apps/knowledge/models.py`, `apps/knowledge/services/retriever.py`,
`apps/recipes/models.py`, `apps/recipes/services/runner.py`, `apps/recipes/api/views.py`,
`apps/agents/tools/artifact_tool.py`, `apps/agents/tools/learning_tool.py` (lines 130-250),
`apps/agents/graph/base.py` (lines 220-660: schema-context assembly, namespace hint,
injection, build_agent_graph), `apps/agents/graph/state.py`,
`apps/workspaces/models.py`, `apps/workspaces/services/schema_manager.py` (all),
`mcp_server/context.py`, `mcp_server/services/query.py`.

**Skimmed (targeted sections / greps):**
`mcp_server/server.py` (lines 40-180: context resolution, list_tables),
`apps/workspaces/api/views.py` (lines 380-540: dictionary/table-detail/annotate; line 362 refresh),
`apps/workspaces/tasks.py` (refresh_tenant_schema region + grep),
`mcp_server/services/sql_validator.py` (schema-allowance logic only),
`mcp_server/services/metadata.py` (row-count key grep), `apps/knowledge/api/views.py`
(import/export region), `apps/chat/views.py` (state-injection grep),
`apps/knowledge/admin.py` (confidence-action grep), frontend greps
(`row_count` consumers), git history of `schema_manager.py`, full diff of `2aaf4fb`,
signature archaeology of `build_agent_graph`, `tests/test_recipes.py:696` (mock seam).

**Not examined (honest gaps for the gap loop):**
- `mcp_server/services/materializer.py` — how *table* names themselves are derived per
  provider; whether loader/dbt changes have ever renamed a table (sibling rename class
  to the view-prefix one).
- `apps/transformations/` + dbt models — model renames leave old relations behind or
  drop them? Stored SQL against replaced/`stg_` models untraced.
- LangGraph checkpointer replay mechanics (`apps/agents/memory/checkpointer.py`,
  resume path in `tasks.py`) — stale names in resumed threads asserted from structure,
  not traced.
- `apps/workspaces/services/workspace_service.py` (`remove_workspace_tenant`) — read
  about only via docstrings/cartography; teardown-side hooks not verified.
- `apps/chat/thread_views.py` / public share pages / `widget.js` — whether shared
  threads or embeds replay artifact queries through yet another context path.
- `apps/artifacts/services/export.py` — whether export re-executes `source_queries`
  via a third resolution path.
- Frontend artifact/dictionary stores beyond grep level.
- v1's refresh-path S1 ("loads into old schema then destroys it") — relied on, not
  re-verified; Finding 5's practical frequency depends on it.
- `tests/test_recipe_runner.py` contents (grep returned no mock/graph lines — file not
  opened; possible it exercises `_build_graph` differently).
- Whether any prod artifacts/learnings actually reference pre-#228 prefixes (no DB
  access — Finding 4 impact is mechanism-verified, population-unverified).
