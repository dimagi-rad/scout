# Seam review: backend ↔ frontend contract

*Reviewer: seam-backend-frontend (architecture review v2, Phase 1). Date: 2026-06-12.
Mandate: own the API contract — every response shape vs the hand-rolled TypeScript
types, UI affordances vs what the backend actually honors, polling/streaming
assumptions. Report only; no code changed.*

The frontend has **no codegen and no shared schema**: every contract lives twice, once
in a Django view's dict literal and once in a hand-written TS interface
(`frontend/src/api/*.ts`, `frontend/src/store/*Slice.ts`). I compared each pair by
reading both sides. The core CRUD seams (workspaces, members, tenants, jobs polling,
knowledge, connections, auth) are in good shape — shapes match field-for-field. The
drift concentrates in three places: **the chat live-stream vs reloaded-transcript
divergence**, **single-tenant compatibility shims serving multi-tenant workspaces**,
and **a graveyard of sharing/public-page surface whose UI was removed 2026-06-04 but
whose types, dead code, and unenforced toggles remain on both sides**.

---

## Findings

### F1. Live-stream tool cards carry the wrong `toolCallId`; per-card progress/Stop/failure affordances never engage during a live session — BROKEN-NOW / correctness / verified-by-trace

The system has two sources of `toolCallId` for the same tool-call part and they do not
agree:

- **Live SSE stream**: `apps/chat/stream.py:190` — `tool_call_id = run_id or
  uuid.uuid4().hex`, where `run_id` is the **LangGraph callback run UUID** of the
  `on_tool_end` event. This is emitted as `toolCallId` in `tool-input-available` /
  `tool-output-available` chunks (stream.py:194, 207).
- **Backend job rows**: `apps/agents/graph/base.py:462–469` injects `tc.get("id")` —
  the **LLM tool-call id** (`toolu_…`) — into MCP tool args as `tool_call_id`;
  `mcp_server/server.py:623–628` persists exactly that on `ThreadJob.tool_call_id`;
  `apps/workspaces/api/jobs_views.py:52` returns it in the active-jobs poll.
- **Reloaded transcript**: `apps/chat/message_converter.py:59` — `"toolCallId":
  tc["id"]` — the LLM id again.

The frontend scopes job affordances per card by equality:
`frontend/src/components/ChatMessage/ChatMessage.tsx:161–163`
(`activeMaterializationJob.tool_call_id === part.toolCallId`) and per-card failure
cards via `recentTerminationsByToolCallId[toolCallId]` (ChatMessage.tsx:412–416).

**Consequence chain** (user runs a materialization from chat and stays on the live
transcript): chat POST → `stream.py` emits the card with `toolCallId = <langgraph
run uuid>` → `useWorkspaceJobs` polls `/jobs/active/` which returns `tool_call_id =
toolu_…` → equality at ChatMessage.tsx:163 is always false → the in-card progress
block (ChatMessage.tsx:271–286), in-card Stop button (`showCancelButton`,
ChatMessage.tsx:202–206), and in-card failure card (`matchingFailure`) **never render
in the live path**. They only work after the transcript is reloaded from the
checkpointer (thread switch, page refresh, or the automatic reload that
`recentlyCompletedThreadIds` triggers in ChatPanel.tsx:126–131 — which is also why the
failure card eventually appears: the reload swaps in the real `toolu_` ids).

Mitigations that keep this from being severe: the thread-level
`MaterializationProgressBanner` matches by `thread_id` (ChatPanel.tsx:43), so progress
and a Stop button do exist during the live session — just not the per-card ones this
machinery was built for (the comments at jobs_views.py:48–51 and jobs.ts:22–27
describe an intent the live path does not deliver). The fix is one line: stream.py has
the real id available as `tool_output.tool_call_id` on the `ToolMessage` it already
handles.

Complexity: accidental. Reachable via: any chat-initiated `run_materialization`.

### F2. Live stream vs reloaded transcript render differently: no tool-start events, empty inputs, and 2000-char truncation that breaks rich tool rendering — BROKEN-NOW / correctness / verified-by-trace

Three independent mechanisms make the live transcript a degraded version of the
reloaded one:

1. **No tool events until the tool finishes.** `stream.py` only handles
   `on_tool_end` (stream.py:159); `tool-input-available` is emitted *after* the tool
   completed (stream.py:191–198). While a long query/materialization dispatch runs,
   the AI-SDK tool part states `input-streaming`/`input-available` that
   `ChatMessage.tsx:153` treats as "loading" never occur live. The model's streamed
   `tool_use` blocks in `on_chat_model_stream` are ignored (only `text`/`thinking`
   blocks are handled, stream.py:124–136).
2. **Inputs are always `{}`** (stream.py:196), so a live card can never show what SQL
   was run; after reload, `message_converter.py:61` supplies the real
   `tc.get("args")`. (Upside, verified: the reloaded args are the *pre-injection*
   LLM args — the copied-message injection in `graph/base.py:452–475` is not
   checkpointed, so `workspace_id`/`user_id`/`tool_call_id` do not leak into the UI.)
3. **Output truncation breaks rich rendering only in the live path.**
   stream.py:200–203 truncates tool output at 2000 chars and appends
   `"... (truncated, N chars total)"`, which makes the payload unparseable JSON.
   `ChatMessage.tsx:24–53` (`parseOutput`) then fails and falls back to raw
   `<pre>` text — so the rich `QueryToolOutput` table (ToolOutput.tsx) almost never
   renders live for real result sets (a pretty-printed envelope with `indent=2`
   crosses 2000 chars at roughly a dozen rows). The converter path
   (message_converter.py:69) does **not** truncate, so the same message renders the
   full rich table after reload.

Consequence: users see a different (worse) transcript during the session than after a
refresh; QA scenarios targeting rich tool output will pass or fail depending on which
path rendered. Complexity: accidental — both paths exist to serve one contract and
have drifted. Reachable via: every chat turn that calls a data tool.

### F3. Data Dictionary serves only the *first* tenant of a multi-tenant workspace — BROKEN-NOW / correctness / verified-by-trace

`apps/workspaces/api/views.py` resolves everything through `workspace.tenant`:
`DataDictionaryView.get` (views.py:241, 245), `TableDetailView.get/put`
(views.py:479–483, 506), `RefreshSchemaView.post` (views.py:336), `RefreshStatusView`
(views.py:387). `Workspace.tenant` is an explicit single-tenant compatibility shim —
`apps/workspaces/models.py:143–146`: *"Single-tenant compatibility: returns the first
associated tenant"* (`self.tenants.first()`, ordering not even guaranteed stable).

For a workspace with 2+ tenants, the agent queries the **workspace view schema**
(union views; `WorkspaceViewSchema`), but the Data Dictionary page
(`frontend/src/pages/DataDictionaryPage`, store `dictionarySlice.fetchDictionary` →
`GET /data-dictionary/`) renders the first tenant's physical schema only: tables from
one source, columns of one schema, annotations keyed `"{first_tenant_schema}.{table}"`
(views.py:289–290). The unioned tables the agent actually reads are never shown, and a
second tenant's tables are invisible. If the first tenant's schema is expired but a
sibling's is ACTIVE, `_schema_unavailable_response` (views.py:40–70) 503s the whole
page even though the workspace is queryable.

Reachable via: `/data-dictionary` for any multi-tenant workspace (multi-tenant
workspaces are a first-class flow — the 2026-06-10 incident cluster was about them).
Complexity: accidental (rename/migration residue: the view predates multi-tenancy).
Confidence: verified-by-trace for the code path; I did not run a live multi-tenant
workspace.

### F4. Live artifact queries in multi-tenant workspaces execute against the first tenant's schema, not the view schema — LATENT / correctness / strong-inference

`ArtifactQueryDataView.get` (`apps/artifacts/views.py:795–800`): `tenant = await
artifact.workspace.tenants.afirst()` → `ctx = await
load_tenant_context(tenant.external_id)` → `execute_query(ctx, sql)` runs with
`search_path` set to that tenant's schema. In a multi-tenant workspace the agent
authored `source_queries` while connected to the **view schema**; replaying them
against tenant #1's physical schema either fails (missing unioned views) or silently
returns a single-tenant subset presented as the artifact's data. The same first-tenant
context backs the sandbox's live fetch (the sandbox JS fetches the same
`query-data` endpoint, views.py:254).

Status LATENT rather than BROKEN-NOW because I did not verify what table names the
agent emits in `source_queries` for view-schema workspaces (if view names match
per-tenant table names, this degrades silently to wrong data — worse). Reachable via:
artifact "Data" tab and any `has_live_queries` artifact render in a multi-tenant
workspace. Complexity: accidental.

### F5. The Data Dictionary refresh button fires the legacy `/refresh/` path and then lies about completion; the status endpoint built for it has zero callers — BROKEN-NOW / correctness (and gateway to a known data-loss path) / verified-by-trace

Chain: `refresh-schema-btn` (`DataDictionaryPage.tsx:95–106`) →
`dictionarySlice.refreshSchema` (dictionarySlice.ts:192–212) → `POST
/api/workspaces/<id>/refresh/` → `RefreshSchemaView.post` (views.py:325–370) returns
**202 Accepted** with `{schema_id, status: "provisioning"}` and defers
`refresh_tenant_schema` — the path v1 run A verified as S1 (loads into the old schema
then destroys it). The frontend ignores the 202 body, immediately re-fetches the
dictionary, and sets `dictionaryStatus: "loaded"` — i.e. the spinner stops and the
user sees (stale) data as if the refresh finished. Nothing ever polls
`GET /refresh/status/`: `grep` over `frontend/src` finds zero references
(`RefreshStatusView`, views.py:373–402, is dead surface). A background refresh
failure is therefore invisible to the user forever.

Bonus UX trap, same chain: the button renders for **all** roles, but the backend
requires read_write/manage (views.py:330). A read-role member clicking it gets a 403,
and `refreshSchema`'s catch replaces the whole loaded dictionary with the full-page
error state ("Failed to load dictionary") — the already-loaded data vanishes.

Reachable via: visible button on `/data-dictionary`. Complexity: accidental.

### F6. Thread sharing contract is three-ways inconsistent; frontend types declare fields the backend never returns — LATENT / correctness / verified-by-trace

- `frontend/src/store/uiSlice.ts:5–14` declares `Thread.is_public: boolean` and
  `Thread.share_token: string | null`. The list endpoint (`_list_threads`,
  `apps/chat/thread_views.py:79–91`) returns **neither** — at runtime both are
  `undefined` while the type says otherwise. The `Thread` **model** has no `is_public`
  at all (grep: `is_public` exists only in `apps/recipes`).
- `uiSlice.updateThreadSharing` PATCHes `{is_shared?, is_public?}` and expects
  `ThreadShareState.is_public` back; the backend handler (`thread_views.py:40–49,
  187–197`) reads only `is_shared` and returns `{id, is_shared, share_token}` —
  `is_public` is **sent but ignored** and **expected but never returned**. No
  component calls `updateThreadSharing` anymore (share UI removed 2026-06-04; grep
  confirms zero callers), so this is a dead action whose store-write would set
  `is_public: undefined` on a thread if ever invoked.
- The share endpoints (`GET/PATCH /threads/<id>/share/`) remain fully live with no UI,
  and the public page `PublicThreadPage` remains routed (App.tsx:21–22) — consistent
  with the known "share surface drift" seed; the seam-specific addition is the
  type-level phantom fields and the request field the backend silently drops.

Complexity: accidental (removal residue). Impact today: type lies + dead code;
becomes live breakage the moment anyone re-wires a share UI against these types.

### F7. Recipe/run "Share with project" toggles persist a flag nothing enforces; run sharing UI cannot actually produce a public link — BROKEN-NOW (expectation mismatch) / correctness / verified-by-trace

- UI promise: `RecipeDetail.tsx:195–222` — checkbox "Share with project: *All project
  members can view and run this recipe*"; same per-run in `RecipeRunDetail.tsx:195–217`
  and `RecipeDetail.tsx:330–340`.
- Backend reality: `RecipeListView.get` (`apps/recipes/api/views.py:34–40`) returns
  `Recipe.objects.filter(workspace=workspace)` — **no `is_shared` filter, no
  created_by filter**; `RecipeRunListView` likewise (views.py:140). Grep across
  `apps/recipes` shows `is_shared` consumed nowhere except serializers/admin/an index.
  Every member sees and can run every recipe regardless of the checkbox; unchecking it
  does not make anything private. The toggle is a stored no-op.
- Adjacent drift: the run's *public* share requires `is_public=True`
  (`PublicRecipeRunView`, views.py:179–183; `share_token` is only generated when
  `is_public` is set, `models.py:388–390`), but no remaining UI sets `is_public` —
  the checkboxes set only `is_shared`. So the still-live public endpoint
  `/api/recipes/runs/shared/<token>/` and `PublicRecipeRunPage` are unreachable for
  any newly-shared run; only pre-2026-06-04 tokens work.
- Terminology residue: the UI still says "project" (workspaces rename, 2026-03-17).

Complexity: accidental. Reachable via: Recipes pages, today.

### F8. `PublicRecipePage` is orphaned on both sides: no route, and it fetches an endpoint that does not exist — DEBT / velocity / verified-by-trace

`frontend/src/pages/PublicRecipePage.tsx:43,55` matches `/shared/recipes/<token>` and
fetches `GET /api/recipes/shared/<token>/`. Neither exists: `App.tsx:19–24` routes only
`/shared/runs/` and `/shared/threads/`; `config/urls.py` + `apps/recipes/urls.py` have
no recipe-level shared endpoint (only `runs/shared/`). `Recipe.is_public` /
`Recipe.share_token` (models.py:71–84) are write-only dead fields. 151 lines of dead
page + two dead model fields.

### F9. Artifact sandbox URLs are root-relative while the labs deployment mounts the app at `/scout/` — LATENT / correctness / strong-inference (deployment half is hypothesis)

Two sites hardcode root-relative API paths, bypassing the `BASE_PATH` discipline every
other call follows (`api/client.ts:30` prefixes all `api.*` calls):

- `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx:192` — iframe
  `src="/api/workspaces/<ws>/artifacts/<id>/sandbox/"` (the only API URL in the SPA
  not built from `BASE_PATH`; grep-verified).
- The sandbox HTML itself, served by Django: `apps/artifacts/views.py:254` —
  `fetch('/api/workspaces/' + …)`.

`.github/workflows/deploy-labs.yml:200` builds with `VITE_BASE_PATH=/scout/` (and
`config.ts:7` documents "connect-labs: /scout"). Unless the labs proxy *also* exposes
`/api/…` at the host root, the artifact View tab 404s there while everything else
works. Confidence: verified for the code; **hypothesis** for the deployment behavior
(I did not read the labs proxy config — flagging for the ops lens / a verifier).

### F10. Table-annotation auto-save silently wipes `related_tables` (asymmetric merge semantics in the PUT handler) — LATENT / correctness / verified-by-trace

`TableDetailView.put` (`apps/workspaces/api/views.py:510–538`) merges some fields and
clobbers others: `description`, `refresh_frequency`, `owner` default to the existing
value (`data.get("description", tk.description)`), but `use_cases`,
`data_quality_notes`, `related_tables`, `column_notes` default to **empty**
(`data.get("related_tables", [])` → `tk.related_tables = related_tables`). The
frontend auto-save (`TableDetail.tsx:157–172`, debounced 1s on changes in any field)
sends `use_cases`, `data_quality_notes`, `refresh_frequency`, `owner`,
`column_notes` — and **never** `related_tables`. So any non-empty `related_tables` is
erased the first time a user touches any annotation field on that table.

Today's only writers of `related_tables` are this endpoint and Django admin
(grep: `apps/knowledge/admin.py:26`), so the data being destroyed is admin-curated;
the reader is the agent-context retriever
(`apps/knowledge/services/retriever.py:95–97`), so the damage is silent degradation of
agent answers. Complexity: accidental.

### F11. Chat is a dead end in a workspace with zero data sources: backend 403, frontend generic error — LATENT / correctness / strong-inference

`CreateWorkspaceModal` explicitly allows creating a workspace with no tenants
(comment at CreateWorkspaceModal.tsx:60: "workspace can still be created without a
data source"; `tenant_ids` defaults to `[]`, workspace_views.py:170). For such a
workspace `_resolve_workspace_and_membership` returns `(workspace, None, False)`
(`apps/chat/helpers.py:110–112`: zero tenants → `tenants.afirst()` is None), and
`chat_view` rejects with 403 `"No tenant membership for this workspace"`
(chat/views.py:113–114). The frontend renders the full chat UI for this workspace and
surfaces the rejection as the generic "Something went wrong. Please try again."
(ChatPanel.tsx `ChatError` — only the stale-thread case gets special handling). No
affordance points the user at "add a data source first". Confidence: strong-inference
(each hop read; the combined journey not executed live).

### F12. `ConnectionsPage` post-removal workspace-switch logic compares two different ID spaces and can never fire — LATENT (dead guard) / correctness / verified-by-trace

`ConnectionsPage.tsx:146–154`: after deleting a connection it builds
`removedMembershipIds` from `chatbots[].membership_id` (**TenantMembership** ids) and
then checks `removedMembershipIds.has(activeDomainId)` — but `activeDomainId` and
`storeDomains[].id` are **workspace** ids (`domainSlice` is fed by
`workspaceApi.list()`). A membership UUID never equals a workspace UUID, so the
"switch away from a now-empty active workspace" recovery is dead code — residue from
the era when "domains" *were* tenant memberships. Consequence: after removing the
connection backing the active workspace, the user stays in a workspace whose data
sources were just archived, with no redirect. Complexity: accidental (rename residue).

### F13. Stream protocol swallows errors: timeouts and exceptions become polite text deltas with `finishReason: "stop"` — DEBT / correctness / verified-by-trace

`stream.py:212–239`: on `TimeoutError` (300s budget) or any exception, the stream
emits an apology **as message text** and then the normal
`finish-step` / `finish {finishReason:"stop"}` (stream.py:248–249). The AI-SDK
`error` state therefore only ever fires for transport-level failures; a failed agent
run is indistinguishable from a successful one to `useChat` (`status === "ready"`),
and ChatPanel's "refresh thread list when streaming finishes" effect
(ChatPanel.tsx:134–139) treats it as success. There is no `error` chunk type in the
emitted protocol at all, though the AI SDK supports one. This is the SSE flavor of the
codebase-wide "failures swallowed → caller told completed" pattern from the
2026-06-10 incident (PR #229 fixed the MCP/agent flavor).

### F14. Dead/decorative surface inventory at this seam — DEBT / velocity / verified-by-trace

Fields and endpoints where one side talks and nobody listens (each grep-verified):

| Item | Side that exists | Side that's missing |
|---|---|---|
| `GET /refresh/status/` (views.py:373) | backend | zero frontend callers (F5) |
| `POST /artifacts/<id>/undelete/`, `GET /artifacts/<id>/export/<fmt>/`, `GET /artifacts/<id>/data/` | backend (`apps/artifacts/urls.py`) | no UI caller (export UI is the sandbox print-to-PDF postMessage path only) |
| `/api/transformations/…` (the only DRF-router surface) | backend | zero frontend references — an entire app with no UI |
| `created_by_name` (artifacts list views.py:871, recipes serializers, knowledge serializer) | backend returns | never rendered; absent from `ArtifactSummary`/`Recipe` TS types |
| `theme` embed param (widget.js:71 → useEmbedParams.ts:25) | both parse it | consumed nowhere (EmbedLayout uses only `mode`) |
| `connected` boolean in `/api/auth/providers/` (auth_views.py:263) | backend returns | frontend keys everything off `status` only |
| `POST /api/auth/tenants/select/` (`tenant_select_view`) | backend | no frontend caller (only `ensure/` is used, by embed) |
| `setActiveDomainByTenantId` (domainSlice.ts:73) | frontend action | explicit no-op |
| `RecipeRun.is_public` plumbing (`recipeSlice.updateRecipeRun` accepts `is_public`) | both | no control sends it (F7) |
| `domainSlice.TenantMembership` alias with phantom `provider`/`tenant_id`/`tenant_name` | frontend type | backend list returns none of them at top level |

### F15. Minor type lies (compile-time contract ≠ runtime payload) — COSMETIC / velocity / verified-by-trace

- `recipeSlice.fetchRecipes` types the list as `Recipe[]`, but `RecipeListSerializer`
  omits `prompt` and `variables` — list items are structurally missing fields the
  type marks required (`RecipeDetail` re-fetches the detail, so no runtime break
  today).
- `ArtifactPanel.QueryResult.sql` is required, but error entries from
  `ArtifactQueryDataView` (`views.py:804, 819, 831`) carry only `{name, error}` —
  rendered `<pre>{query.sql}</pre>` shows nothing rather than crashing, by luck.
- `ChatMessage.parseOutput` (ChatMessage.tsx:28) starts by replacing every `'` with
  `"` to handle Python-repr'd envelopes — corrupts any payload containing an
  apostrophe before falling back; survives only because the fallback re-parses the
  raw string. Fragile parser for an uncontracted wire format.
- `workspaceApi.create` response omits `schema_status`/`last_synced_at` that
  `WorkspaceListItem` requires; callers re-fetch the list, so latent only.

---

## Polling / streaming assumptions (checked, mostly sound)

- **Jobs polling**: `useWorkspaceJobs` polls `/jobs/active/` every 3s; the response
  shape (`jobs`, `recent_terminations`) matches `ActiveJobsResponse` exactly,
  including `progress.unit` and `retry_available` semantics
  (jobs_views.py:25–79 ↔ jobs.ts:7–48). The API-side stale-job reconciliation
  backstop (jobs_views.py:122–135, the 2026-06-09 incident fix) means the poll
  self-heals a dead worker — good.
- **Completion detection** is diff-based on thread ids leaving the active set
  (useWorkspaceJobs.ts:51–58) and resets correctly on workspace switch
  (useWorkspaceJobs.ts:79–84, the cross-workspace fix).
- **SSE**: `text/event-stream` + `X-Accel-Buffering: no` + CSRF header via transport
  factory; AI SDK v6 request shape (`data.workspaceId/threadId` via a `body()` closure
  reading a ref) matches what `chat_view` parses (chat/views.py:84–86). The backend
  ignores the rest of the AI-SDK payload (`id`, `trigger`, `messageId`, the full
  `messages` history — only the last user message is used; history comes from the
  checkpointer). Fine, but it means client-side message edit/regenerate features
  would silently do nothing if ever enabled.
- **Thread-switch flow** (the 2026-06-10 threadId-leak fix): `setActiveDomain`
  resets `threadId` (domainSlice.ts:53–70), `thread_messages_view` 404s stale/foreign
  threads vs `[]` for brand-new ones (thread_views.py:146–156), ChatPanel recovers on
  404 (ChatPanel.tsx:101–109). Coherent end-to-end; no regression found.

## What's fine (verified pairs)

- **Workspace list/detail/create/patch/members/tenants** ↔ `api/workspaces.ts`: every
  field matches, including `schema_status` derivation shared between list and detail
  (`_derive_schema_status`, workspace_views.py:33) and the frontend mapping
  `failed → empty` (workspaces.ts:79–92) matching the post-#229 backend semantics.
  `members/<int:…>/` URL converter matches `WorkspaceMembership`'s int pk.
- **Knowledge** list/create/update/delete/export/import ↔ `knowledgeSlice`: pagination
  envelope, `type` discriminator injected by serializers, entry/learning field sets
  all match; learning-create blocked server-side and not offered client-side.
- **Connections / api-key-providers / providers** ↔ ConnectionsPage +
  ApiConnectionDialog: shapes match including the dynamic form-field schema and the
  OCS `team_name` fallback field; `team_slug`/`team_name` property-over-JSON
  persistence in `_persist_api_key_connection` is correct (checked the
  `update_fields=["provider_metadata"]` suspicion — they're properties; it's right).
- **Auth** `me`/`login`/`csrf` ↔ `authSlice.User`: exact match; `csrftoken_scout`
  cookie name matches the client regex (settings base.py:335 ↔ client.ts:8).
- **Recipes / runs** core shapes (status enum, step_results, run fields) match; run
  PATCH accepts exactly what the slice sends.
- **Public thread page** ↔ `public_thread_view` payload: exact match.
- **materialize/retry + jobs/cancel** ↔ frontend callers: response fields match;
  retry thread-binding validation (materialization_views.py:159–182) is solid.
- **No leakage of injected MCP params** into reloaded transcripts (the injecting tool
  node modifies a copy; checkpoints keep the LLM-authored args).
- **widget.js** ships in the API image (`COPY . .` in Dockerfile) and detects its base
  path from the script src, so the `/scout` prefix works for the widget↔embed
  handshake.

## Coverage log

**Deep-read (line-by-line):**
frontend: `api/client.ts`, `api/workspaces.ts`, `api/jobs.ts`, `api/auth.ts`,
`api/threads.ts`, `api/userTenantsCache.ts`, `store/uiSlice.ts`,
`store/domainSlice.ts`, `store/authSlice.ts`, `store/artifactSlice.ts`,
`store/dictionarySlice.ts`, `store/knowledgeSlice.ts`, `store/recipeSlice.ts`,
`components/ChatPanel/ChatPanel.tsx`, `components/ChatMessage/ChatMessage.tsx`,
`components/ChatMessage/ToolOutput.tsx` (first 120 lines),
`components/MaterializationStatus/*` (both), `hooks/useWorkspaceJobs.ts`,
`hooks/useWorkspaceThreadSync.ts`, `hooks/useEmbedParams.ts`,
`pages/ConnectionsPage/ConnectionsPage.tsx`, `components/ApiConnectionDialog/*`,
`pages/DataDictionaryPage/DataDictionaryPage.tsx`,
`pages/DataDictionaryPage/TableDetail.tsx` (save path),
`pages/WorkspaceDetailPage` (TenantsTab + API-call inventory),
`pages/PublicThreadPage.tsx`, `pages/PublicRecipePage.tsx` (head),
`pages/RecipesPage/RecipeDetail.tsx` (sharing sections),
`pages/RecipesPage/RecipeRunDetail.tsx` (sharing section),
`components/ArtifactPanel/ArtifactPanel.tsx`, `pages/EmbedPage.tsx`,
`components/EmbedLayout/EmbedLayout.tsx`, `App.tsx`, `router.tsx`, `main.tsx`,
`config.ts`, `public/widget.js` (head + URL contract).
backend: `apps/workspaces/api/workspace_views.py`, `apps/workspaces/api/views.py`,
`apps/workspaces/api/jobs_views.py`, `apps/workspaces/api/materialization_views.py`,
`apps/workspaces/api/urls.py`, `config/urls.py`, `config/views.py`,
`apps/chat/views.py`, `apps/chat/stream.py`, `apps/chat/thread_views.py`,
`apps/chat/message_converter.py`, `apps/chat/helpers.py`,
`apps/recipes/api/views.py`, `apps/recipes/api/serializers.py`,
`apps/recipes/urls.py`, `apps/knowledge/api/views.py`,
`apps/knowledge/api/serializers.py`, `apps/users/views.py`,
`apps/users/auth_views.py`, `apps/artifacts/views.py` (lines 240–310 sandbox JS,
680–760 skim, 773–917 query-data/list/detail), `apps/artifacts/urls.py`,
`mcp_server/envelope.py`, `mcp_server/server.py:515–650` (run_materialization),
`apps/agents/graph/base.py:430–530` (injection + graph build head), selected model
regions (`workspaces/models.py` Workspace/WorkspaceMembership/WorkspaceViewSchema,
`recipes/models.py` field-level greps, `users/models.py` team-field region,
`api_key_providers/ocs.py:40–90`).

**Skimmed:** `Sidebar.tsx` (thread-field usage grep), `WorkspaceSwitcher.tsx` /
`WorkspacesPage.tsx` (field-consumption grep only), `CreateWorkspaceModal.tsx`
(grep + key lines), `Dockerfile`, `Dockerfile.frontend`, `deploy-labs.yml`
(VITE_BASE_PATH grep), `RecipesPage.tsx`, remainder of `apps/artifacts/views.py`.

**Not examined (honest gaps for the gap loop):**
- `tests/` entirely (including `tests/qa/`) — whether any test pins these contracts.
- `apps/chat/checkpointer.py`, `rate_limiting.py`, `constants.py`; 429 handling
  end-to-end.
- `apps/artifacts/services/export.py` and the full sandbox HTML/CSP
  (views.py lines 1–240, 310–680).
- `apps/recipes/services/runner.py` (sync execution inside a request — out of scope
  here; not assessed).
- `mcp_server/` beyond `envelope.py` + `run_materialization`; the other 10 tool
  response shapes vs `ToolOutput.tsx`'s `DescribeTableOutput`/`ListTablesOutput`/
  `GetMetadataOutput` interfaces — **a direct extension of this mandate I did not
  finish**.
- `frontend/src`: `KnowledgeForm/KnowledgeList/KnowledgePage` internals,
  `RecipeRunner`, `ArtifactList` internals, `SchemaTree`, `OnboardingWizard`,
  `LoginForm`, `useEmbedMessaging`, `useAutoResize`, `NetworkStatusContext`,
  `threadStorage`, `slashCommands`, `ChatRoute`/`ChatRedirect`, `ChatEmptyState`,
  `lib/workspacePath`, remainder of `WorkspaceDetailPage` (members tab UI, settings
  tab), `ToolOutput.tsx` lines 120+.
- The labs/production proxy config (needed to settle F9's deployment half).
- allauth flow pages, `setup_oauth_apps`, OAuth `next=` redirect handling.
- Live verification of any finding in a running stack (all findings are code-trace).
