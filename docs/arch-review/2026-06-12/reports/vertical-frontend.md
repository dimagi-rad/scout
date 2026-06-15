# Vertical review: Frontend (full app)

*Reviewer: vertical:frontend · 2026-06-12 · HEAD 35e4230*
*Scope: `frontend/src/` (117 files, ~13.1k LOC), `frontend/public/widget.js`, nginx/vite/deploy
serving of the SPA, and the backend response shapes the frontend types claim to mirror.*

Evidence standards followed: every BROKEN-NOW claim below has a quoted entry-point → call-path →
consequence chain; confidence is labeled per finding; reachability checked against routes/UI;
comments treated as claims and verified against logic.

---

## 0. Architecture as-built (short)

- **One zustand store** (`store/store.ts`) composed of 7 slices. Slices are flat-merged; cross-slice
  access is done by casting `get()`/`set()` (`domainSlice.ts:63`, `uiSlice.ts:55`) — works, but the
  workspace/thread coupling is invisible to the type system.
- **Two browser routers**: the main app (`router.tsx`, mounted by `App.tsx`) and a second, almost
  identical route table for the embed (`pages/EmbedPage.tsx:20-50`). Public share pages bypass
  routing entirely — `App.tsx:19-24` regex-matches `window.location.pathname` before the router
  mounts. Three routing regimes in one SPA.
- **Polling, not push**, for everything except the chat SSE stream: jobs every 3s
  (`useWorkspaceJobs.ts:4`), health every 5s (`NetworkStatusContext.tsx:15`).
- **Hand-written TS types** in `src/api/*` and slices mirror backend JSON by convention only; no
  codegen, no runtime validation. `tsc -b` is clean — which proves nothing about wire fidelity, and
  the three contract breaks below all type-checked.
- **Vocabulary residue**: the store still says "domains" (`domainSlice`, `activeDomainId`,
  `TenantMembership` alias at `domainSlice.ts:6` "kept so code referencing these doesn't break")
  for what the product now calls workspaces. One UI string still leaks it: `ChatPanel.tsx:184`
  renders "Select a domain to start chatting".

### Capability scorecard (% actually functional, demo path vs edges)

| Capability | % functional | Notes |
|---|---|---|
| Chat + SSE streaming + thread restore | ~90% | Solid post-incident; degradations: F10 parse hack, stop is client-abort only |
| Workspace↔thread↔URL sync | ~90% | The 2026-06-10 threadId-carryover fix (00c423d) verified correct end-to-end |
| Jobs polling / materialization progress UI | ~90% | Honest indicators (real % or indeterminate); cost note F12 |
| Workspace management (members/tenants/settings) | ~85% | Server-side role checks exist on these routes; F16 sticky error |
| Onboarding wizard | ~50% | OAuth path works; **API-key path is 100% broken (F1)** |
| Connections page | ~85% | Dead active-workspace-reset logic (F8) |
| Artifacts (list/view/data tab) | ~90% root deploy / view-tab **0% in labs** (F2b) | Export PDF = iframe print only; backend `export/<format>` unconsumed |
| Recipes | ~70% | Run/edit work; "Share with project" wired to nothing (F6); stale on workspace switch (F3) |
| Knowledge | ~80% | Works; pagination metadata with no pagination UI (F13) |
| Data dictionary | ~75% | Refresh button drives the flagged legacy `/refresh/` path (F5); annotation autosave wipes `related_tables` (F4) |
| Public share pages | ~40% of what the code implies | No creation UI for thread shares; recipe public page dead (F7); broken under `/scout` (F2c) |
| Embed / widget SDK | ~85% | set-tenant/resize/popup-oauth coherent; artifact panel broken in labs (F2b); hardcoded `/labs/scout/` fallback (F14) |

---

## Findings

### F1 — BROKEN-NOW · correctness · verified-by-trace
**Onboarding "Use an API Key" posts to an endpoint that was deleted in the TenantConnection rebuild; every non-OAuth new user dead-ends with a 404.**

Chain:
1. `App.tsx:65` — any authenticated user with `!user.onboarding_complete` is forced into
   `<OnboardingWizard />`. Backend: `apps/users/auth_views.py:72-90` — `onboarding_complete` is
   false whenever the user has no active connection-backed `TenantMembership` (i.e. every fresh
   email/password signup).
2. `OnboardingWizard.tsx:27-32` — submit handler:
   `await api.post("/api/auth/tenant-credentials/", { provider: "commcare", tenant_id: domain, tenant_name: domain, credential: \`${username}:${apiKey}\` })`.
3. `apps/users/auth_urls.py` — there is **no** `tenant-credentials/` path. The connections rebuild
   (`ad56a65`, PR #220, 2026-06-05) replaced it with `connections/` taking a different payload
   shape (`{provider, fields:{...}}`, see `apps/users/views.py:220-247`). `config/urls.py:99` is
   the only mount of auth urls.
4. Consequence: Django 404 → `client.ts:37-39` throws → user sees "Failed to save credentials"
   with no path forward except finding the OAuth button.

Git: wizard last touched `bee28a8` (BASE_PATH era); endpoint removed `ad56a65`. The wizard also
only offers CommCare, while the modern dialog (`ApiConnectionDialog.tsx`) supports the full
provider strategy registry — the whole component is a pre-#220 fossil sitting on the critical
first-run path. Complexity: accidental. Reachable via: every fresh signup without OAuth.

### F2 — BROKEN-NOW in the `/scout` (labs) deployment · correctness · a: strong-inference, b/c: verified-by-trace
**Three hardcoded root-absolute URLs skip the BASE_PATH prefix; all three break (or lie) when the SPA is mounted at `/scout/`, which is a real deployment (`.github/workflows/deploy-labs.yml:200` builds with `VITE_BASE_PATH=/scout/`).**

All other requests are prefixed centrally (`client.ts:30` `const prefixedUrl = url.startsWith("/") ? \`${BASE_PATH}${url}\` : url`). Three call sites bypass the client:

- **(a) Health poll monitors the wrong server.** `NetworkStatusContext.tsx:29`
  `fetch("/health/", ...)` — under labs this hits `https://<labs-host>/health/`, not Scout.
  `frontend/nginx.prod.conf` maps only `location /scout/health/` to Django; bare `/health/` is
  served by whatever else lives at the labs root. If that returns 200, the online indicator is
  permanently green regardless of Scout's actual API health (a dishonest indicator — the exact
  class the 2026-06-09 incident postmortem cared about: "UI stuck at Preparing…" with no offline
  signal); if it 404s, the app shows a permanent "Server unreachable" banner and the
  `networkStatus === "online"` guards in DataDictionary/Recipes/Knowledge/Artifacts pages
  (`DataDictionaryPage.tsx:72` etc.) suppress every error state. Either branch is wrong.
  (Root-mounted Kamal prod is fine: `nginx.prod-kamal.conf:106` has `location /health/`.)
- **(b) Artifact sandbox iframe 404s.** `ArtifactPanel.tsx:192`
  `src={\`/api/workspaces/${activeDomainId}/artifacts/${artifactId}/sandbox/\`}` — vite's `base`
  rewrites built assets, not runtime strings; under labs this requests `/api/...` which no
  `location` block routes to Django (`nginx.prod.conf` proxies only `/scout/api/`). The artifact
  "View" tab — including inside the embed widget, which is the labs product surface
  (`EmbedLayout.tsx:26` shows `<ArtifactPanel />` for `chat+artifacts`/`full` modes) — renders a
  404 page in the iframe.
- **(c) Public share pages spin forever.** `App.tsx:15-16` strips BASE_PATH to *route* to the page,
  but `PublicThreadPage.tsx:58` and `PublicRecipeRunPage.tsx:51` re-derive the token from the
  **unstripped** `window.location.pathname` (`/^\/shared\/threads\/([^/]+)/` vs actual
  `/scout/shared/threads/<tok>`), so `token` is `undefined`, the effect returns early
  (`PublicThreadPage.tsx:201` `if (!token) return`), `loading` never clears, and the user gets an
  infinite skeleton.

Impact: correctness; status BROKEN-NOW for the labs deployment, LATENT for any future prefixed
mount. Complexity: accidental (one prefixing convention, three exceptions). Fix shape is trivial
(route through `BASE_PATH` like everything else), which is exactly why it keeps regressing —
nothing enforces it.

### F3 — BROKEN-NOW · correctness · verified-by-trace
**Artifacts and Recipes pages do not react to workspace switches: they keep showing the previous workspace's items, and acting on them then targets the new workspace's API scope (404s).**

Chain (recipes; artifacts is isomorphic):
1. User is on `/recipes`; `RecipesPage.tsx:45-47` fetches once: `useEffect(() => { fetchRecipes() }, [fetchRecipes])` — zustand actions are stable references, so this runs only on mount; `activeDomainId` is not a dependency.
2. User switches workspace in the TopBar switcher (rendered on every page, `TopBar.tsx:23`);
   `WorkspaceSwitcher.tsx:260-274` calls `setActiveDomain(ws.id)` and navigates **only** when on a
   `/workspaces/...` path — on `/recipes` the page stays mounted and no refetch occurs.
3. The list on screen still belongs to workspace A. Clicking Run/View calls
   `recipeSlice.fetchRecipe` (`recipeSlice.ts:96-101`) which reads the **new** `activeDomainId`
   from the store at call time → `GET /api/workspaces/<B>/recipes/<recipe-of-A>/` →
   `apps/recipes/api/views.py:55-57` filters `workspace=workspace` → 404 "Recipe not found".
   For artifacts, `ArtifactList` → `openArtifact(id)` → `ArtifactPanel.tsx:192` sandbox URL with
   workspace B + artifact of A → 404 in the iframe.

Contrast: `KnowledgePage.tsx:56` and `DataDictionaryPage.tsx:27` both correctly depend on
`activeDomainId`. Same problem, four pages, two solutions — the two that predate the
workspace-switcher redesign were never migrated. Complexity: accidental.
Reachable via: TopBar switcher on `/artifacts` or `/recipes`.

### F4 — LATENT · data-loss (annotation scope) · verified-by-trace
**Editing any table annotation in the Data Dictionary silently wipes that table's `related_tables` (and would wipe `column_notes`/list fields if they ever arrive by another writer mid-session), because the autosave payload omits fields the backend PUT treats as "reset to empty".**

Chain:
1. `TableDetail.tsx:157-166` — debounced autosave builds
   `{use_cases, data_quality_notes, refresh_frequency, owner, column_notes}` — **no
   `related_tables`, no `description`** — and fires 1s after any keystroke.
2. `dictionarySlice.ts:252` PUTs it to `/data-dictionary/tables/<schema>.<table>/`.
3. `apps/workspaces/api/views.py:537-553` — `related_tables = data.get("related_tables", [])` then
   unconditionally `tk.related_tables = related_tables` and `tk.save()`. (`description` happens to
   survive because its default is `tk.description`; the list/dict fields don't get that courtesy.)
4. Consequence: any `related_tables` content (written via knowledge import, admin, or agent) is
   destroyed the first time a user touches an annotation field in the UI.

The PUT is read-modify-write for some fields and clobber-with-default for others — a contract
nobody wrote down on either side. Reachable via: Data Dictionary → select table → type in any
annotation box. Complexity: accidental.

### F5 — DEBT (reachability confirmation for a known S1) · correctness · verified-by-trace
**The legacy `/refresh/` path flagged S1 by v1 run A is live and one click away in the UI, and the frontend treats it as an instant success.**

- `DataDictionaryPage.tsx:95-106` — `refresh-schema-btn` → `handleRefresh` →
  `dictionarySlice.ts:192-211` `refreshSchema()` → `POST /api/workspaces/<id>/refresh/`
  (`apps/workspaces/api/urls.py:22` → `RefreshSchemaView`).
- The slice then *immediately* re-fetches the dictionary and sets `dictionaryStatus: "loaded"` —
  the spinner stops and the user sees the **pre-refresh** schema presented as the refreshed
  result, while the background task does whatever `refresh_tenant_schema` does (the
  load-into-old-schema-then-destroy-it behavior is the materialization vertical's claim; my claim
  here is reachability + dishonest completion UX).
- `refresh/status/` (`RefreshStatusView`) exists for exactly this, and **no frontend code calls
  it** (grep: zero hits in `frontend/src`). Also unconsumed by the SPA:
  `materialization/cancel/`, artifact `undelete/`, artifact `export/<format>/` (the 472-LOC
  `services/export.py` surface — the UI's "Export PDF" is a client-side iframe `window.print()`,
  `ArtifactPanel.tsx:42-50`).

### F6 — DEBT · correctness (affordance wired to nothing) · verified-by-trace
**The recipe "Share with project" checkbox does nothing: `Recipe.is_shared` is never consulted by any queryset.**

- UI: `RecipeDetail.tsx:205-219` checkbox "Share with project — All project members can view and
  run this recipe" toggles `is_shared`; per-run "Project" checkboxes likewise
  (`RecipeDetail.tsx:332-343`).
- Backend: `apps/recipes/api/views.py:38` `Recipe.objects.filter(workspace=workspace)` — every
  member sees every recipe regardless. `grep is_shared apps/recipes/ --include=*.py` outside
  serializers/admin/models: zero filter sites. Runs the same.
- So the checkbox persists a bit that only the Django admin can see. Users acting on the implied
  privacy model ("unchecked = private to me") are wrong — everything is already visible to the
  whole workspace. Borderline expectations/security; labeling correctness.

### F7 — DEBT · velocity (dead paths & type drift cluster) · verified-by-trace
**The share-surface removal (2026-06-04) and the workspace-model migration left a layer of dead frontend code whose types have already drifted from the backend:**

- `pages/PublicRecipePage.tsx` — not imported by `App.tsx` (only `PublicRecipeRunPage` and
  `PublicThreadPage` are, lines 10-11), no route matches it, and it fetches
  `/api/recipes/shared/<token>/` which **does not exist** (`config/urls.py:102` has only
  `api/recipes/runs/shared/`). Dead component calling a phantom endpoint.
- `uiSlice.ts:34-38 updateThreadSharing` — zero UI callers (thread share menu removed); its
  declared return type `ThreadShareState { is_public }` doesn't match the backend
  (`thread_views.py:178-185` returns `{id, is_shared, share_token}`; `Thread` model has **no**
  `is_public` field at all, `chat/models.py:8-48`).
- `uiSlice.ts Thread` type declares `is_public: boolean; share_token: string | null` but the list
  endpoint (`thread_views.py:79-91`) returns neither — those fields are `undefined` at runtime in
  every `threads` array element. Typed lies.
- `domainSlice.ts:72-75 setActiveDomainByTenantId` — an explicit no-op stub with zero callers.
- `components/TopBar/TopBarSlot.tsx` — portal slot exported, never used by any page.
- `domainSlice.ts:6-11` `TenantMembership` alias with "legacy compat" optional fields — its one
  remaining genuine consumer class is the bug in F8.

Individually cosmetic; collectively this is the rename-residue pattern the cartography flags, and
it's what made F1 possible (dead code keeps compiling because the types are hand-rolled).

### F8 — LATENT · correctness · verified-by-trace
**ConnectionsPage's "reset active workspace after removing the connection that backed it" logic can never fire: it compares workspace ids against TenantMembership ids.**

`ConnectionsPage.tsx:139-154`: `removedMembershipIds` is built from `cb.membership_id`
(= `str(TenantMembership.id)`, `apps/users/views.py:203`), then tested with
`removedMembershipIds.has(activeDomainId)` where `activeDomainId` is a **workspace** id
(`workspaceApi.list()` items). Disjoint id spaces → the condition is always false → after removing
the connection behind the active workspace, the app keeps that workspace active with a dead data
source. (Additionally `storeDomains` in the closure is the pre-refetch render snapshot, so even
matching ids would pick from a stale list.) Residue of the old "domains = tenant memberships"
model. Reachable via: Settings → Connected Accounts → Remove connection while one of its chatbots'
workspaces is active. Consequence is mild (workspace switcher still works), which is why it's
latent, not broken-now.

### F9 — LATENT · correctness (honest-indicator violation) · verified-by-trace
**A multi-tenant workspace that has never been materialized reports `schema_status="provisioning"` forever, which the UI renders as a perpetual "Loading data…" spinner.**

- Backend: `apps/workspaces/api/workspace_views.py:33-54 _derive_schema_status` — for
  `tenant_count > 1`, anything that isn't an ACTIVE or FAILED view schema returns
  `"provisioning"`; a never-synced multi-tenant workspace has **no** `WorkspaceViewSchema` row
  (`view_states.get(w.id)` → `None`, line 104) → "provisioning".
- Frontend: `api/workspaces.ts:79-92 workspaceDataState` maps `provisioning → "loading"`;
  `WorkspaceSwitcher.tsx:56-88 DataIndicator` renders an animated spinner titled "Loading data…".
- Consequence: create a workspace from two tenants and don't chat — the switcher and workspaces
  list show an infinite spinner implying work is happening. This violates the project's own
  honest-progress norm (cartography seed 16). Single-tenant workspaces correctly say
  "unavailable"/empty.

### F10 — DEBT · correctness (silent degradation) · verified-by-trace
**Tool-output rendering depends on a `'`→`"` global replace to parse Python-repr-shaped MCP envelopes; any apostrophe in real data corrupts the parse and rich rendering silently downgrades to a raw text dump.**

`ChatMessage.tsx:24-39 parseOutput`: `const jsonLike = output.replace(/'/g, '"')` then
`JSON.parse`. A value like `St. John's` breaks the parse (fallback: plain `<pre>`), and in
principle a value containing both quote kinds can parse into *wrong* JSON rather than failing.
This is a frontend patch over a backend serialization leak (tool content occasionally arriving as
Python-repr text rather than JSON); the real fix belongs at the producer. Reachable via: any
`query` result containing an apostrophe. Severity is low (display only) but it makes the rich tool
cards unreliable in exactly the demos they exist for.

### F11 — LATENT · correctness · verified-by-trace
**WorkspaceDetailPage never clears a previous error, so after one failed load every subsequent successful load still renders the error screen until remount.**

`WorkspaceDetailPage.tsx:963-977`: the fetch effect does `setLoading(true)` but no
`setError(null)`; render gate at line 994 is `if (error || !workspace)`. Navigate to a dead
workspace URL (403/404), then use the switcher gear to a valid workspace — same route element,
no remount, `error` still set → error page despite `workspace` being loaded. Reachable via:
any stale bookmarked workspace link followed by in-page navigation.

### F12 — DEBT · cost-perf · verified-by-trace
**Three always-on polling loops per tab regardless of visibility or need.** Jobs every 3s
(`useWorkspaceJobs.ts:86-90`, mounted at layout level for every page), health every 5s
(`NetworkStatusContext.tsx:61`), and each jobs poll triggers the API-side stale-job
reconciliation sweep (`jobs_views.py:118-135` runs `reconcile_stale_thread_job` per stale job
*on every poll*). No `document.visibilityState` gating anywhere. With "hundreds of workspaces"
ambitions and idle dashboards left open, this is ~1,700 requests/hour/tab as a floor.
Essential complexity (no push channel exists), accidental cost (no backoff/visibility gate).

### F13 — LATENT · correctness · verified-by-trace
**Knowledge pagination exists in the API and the store, but no pagination controls exist — items beyond the first 50 are unreachable except via server-side search; deep links to them silently no-op.**

`apps/knowledge/api/views.py:23 DEFAULT_PAGE_SIZE = 50`; `knowledgeSlice` stores
`knowledgePagination` (lines 58-78) which **no component reads** (grep: zero non-store hits);
`KnowledgeList.tsx` contains no paging UI. Also `KnowledgePage.tsx:65-73`: the `/knowledge/:id`
deep link opens the edit form only if the item is in the currently loaded page
(`knowledgeItems.find`), otherwise nothing happens — no error, no fetch-by-id.

### F14 — DEBT · velocity · verified-by-trace
**Host-specific hardcode in the generic login flow:** `LoginForm.tsx:48-50` — in embed mode with a
blank referrer, the OAuth `next` falls back to the literal `"/labs/scout/"`, a ConnectLabs path
baked into the product SPA. Works today for the one host; wrong for every other embedder.

### F15 — COSMETIC · velocity
**The repo's own layout rule is drifted from the code it cites as the exemplar.**
`.claude/rules/frontend-layout.md` mandates full-width `p-6` pages "used by Knowledge, Recipes,
Connections" — but `KnowledgePage.tsx:139`, `RecipesPage.tsx:154/214` and `ArtifactsPage.tsx:40`
all use `container mx-auto px-8 py-8` (the pattern the rule forbids). Rule or code, one of them is
wrong; today the rule misleads contributors and review tooling.

---

## Verified-fine (worth recording — these were the incident hot spots)

- **Cross-workspace threadId carryover (incident 1c) is genuinely fixed, in depth.** Four
  cooperating layers, all traced: `domainSlice.setActiveDomain` regenerates `threadId` on
  workspace change (`domainSlice.ts:53-70`); `useWorkspaceThreadSync` adopts URL→store and
  store→URL with a synced-pair ref that prevents ping-pong (`useWorkspaceThreadSync.ts:54-96`);
  `ChatRedirect` restores only the per-workspace localStorage thread, explicitly refusing the
  store's possibly-foreign `threadId` (`ChatRedirect.tsx:28-40`); and the backend distinguishes
  "no Thread row → [] 200" from "row exists elsewhere → 404" (`thread_views.py:146-156`) with a
  matching client recovery path (`ChatPanel.tsx:101-109` → fresh thread, guarded localStorage
  clear in `threadStorage.ts:28-31`). I tried to construct a loop (404 → newThread → reload) and
  cannot: a fresh UUID has no row, so the reload returns `[] 200`. Also `chat/views.py:119-142`
  rejects POSTs to foreign threads server-side.
- **Jobs/progress contract is faithful.** `api/jobs.ts` types match `jobs_views.py:_job_to_dict`
  and `_termination_to_dict` field-for-field, including lowercase `TextChoices` values
  (`chat/models.py:62-67`) and the `tool_call_id` scoping that pins progress/Stop to the right
  tool card (`ChatMessage.tsx:161-165`). Progress bars are honest: determinate only when
  `rows_total` exists, indeterminate sweep otherwise (`MaterializationProgressBanner.tsx:111-128`).
- **Polling single-ownership**: `WorkspaceJobsProvider` is the only `useWorkspaceJobsImpl` caller;
  the cross-workspace `prevThreadIdsRef` reset (`useWorkspaceJobs.ts:79-84`) correctly prevents
  false "just completed" events after a switch.
- **MCP envelope → ToolOutput types** (`success/data/error/warnings/timing_ms/schema`) match
  `mcp_server/envelope.py:34-67`.
- **SSE wire format**: `chat/stream.py` chunk types are the AI-SDK v6 UI Message Stream protocol
  that `DefaultChatTransport` consumes; transport body uses a ref to avoid the stale-closure trap
  (`ChatPanel.tsx:47-58`).
- **Workspace list/detail payloads** match `WorkspaceListItem`/`WorkspaceDetail` types; list order
  is stable (`WorkspaceMembership.Meta.ordering = ["created_at"]`).
- **Widget SDK ↔ embed messaging contract** is consistent both directions (parent→iframe
  `{type, payload}` read at `useEmbedMessaging.ts:43`; iframe→parent flattened `{type, ...payload}`
  read as `data.height` at `widget.js _onMessage`), with origin checks on both ends; `scout:resize`
  is actually emitted (`useAutoResize.ts:23`), contrary to my initial suspicion.
- **Server-side role enforcement exists on every workspace-management route the UI exposes**
  (members PATCH/DELETE, tenant POST/DELETE, settings PATCH — `workspace_views.py:302,333,464,554,599`),
  so the `isManager` UI gating is decoration over real checks, not the only line of defense (the
  broader roles-unenforced claim concerns other routes; these specific ones are fine).
- **CSRF cookie name parity**: `csrftoken_scout` in both `client.ts:8` and
  `config/settings/base.py:335`.

## Cross-cutting observations for other reviewers

- **The backend↔frontend seam has no contract enforcement at all** (no codegen, no zod, no shared
  schema). F1, F7, F8 are all the same disease: the backend moved, `tsc` stayed green. Fix-class:
  generate types from DRF/OpenAPI or add runtime parsing at `client.ts`.
- **BASE_PATH discipline is convention-only** (F2): one central prefixer plus N hand-rolled
  exceptions. Any new `fetch`/`src`/`href` is a roll of the dice; labs is the deployment that pays.
- **`stop()` in chat is a client-side fetch abort** (`ChatPanel.tsx:252`); whether the LangGraph
  run is actually cancelled server-side on disconnect is for the chat/agent vertical to rule on.
- **`message_converter.py` (checkpointer → UIMessage) was not audited by me** — it is the other
  half of the thread-restore contract and should be owned by the chat vertical or seam #4.
- **Artifact sandbox**: `ArtifactPanel.tsx:72-80` accepts `artifact-query-data` postMessages with
  no `event.origin` check, and the sandbox iframe runs LLM-generated code with
  `allow-scripts allow-same-origin` on the app origin (`ArtifactPanel.tsx:194`) — flagging for the
  security lens / artifacts vertical; same-origin + scripts means the sandbox attribute is not a
  security boundary at all here.

## Coverage log

**Deep-read (line-by-line):**
`frontend/src`: router.tsx, App.tsx, main.tsx, config.ts; store/ (all 9 files); api/ (all 6);
hooks/useWorkspaceThreadSync.ts, useWorkspaceJobs.ts, useEmbedParams.ts, useEmbedMessaging.ts,
useAutoResize.ts; contexts/ (both); components/AppLayout, Sidebar/Sidebar.tsx, TopBar/TopBar.tsx,
WorkspaceSwitcher, ChatPanel/{ChatPanel,ChatRoute,ChatRedirect,threadStorage,slashCommands},
ChatEmptyState/ChatEmptyState.tsx, ChatMessage/ChatMessage.tsx, MaterializationStatus/ (both),
ArtifactPanel, EmbedLayout, OfflineBanner, OnboardingWizard, LoginForm, ApiConnectionDialog,
ErrorBoundary; pages/EmbedPage.tsx, ConnectionsPage, WorkspaceDetailPage, DataDictionaryPage.tsx,
RecipesPage.tsx, RecipeDetail.tsx (lines 60-360), KnowledgePage.tsx, PublicThreadPage.tsx,
PublicRecipeRunPage.tsx, WorkspacesPage.tsx (lines 1-180), ArtifactsPage.tsx (lines 1-60);
lib/workspacePath.ts, recentWorkspaces.ts; public/widget.js.
Backend contract surfaces: apps/chat/thread_views.py (full), apps/chat/models.py (lines 1-100),
apps/chat/stream.py (lines 1-120), apps/users/auth_urls.py (full), apps/users/auth_views.py
(lines 30-130), apps/users/views.py (lines 189-249), apps/workspaces/api/jobs_views.py
(lines 1-140), apps/workspaces/api/workspace_views.py (status derivation, list, members, tenants),
apps/workspaces/api/urls.py, apps/workspaces/api/views.py (annotation serialize + table PUT),
apps/recipes/api/{views,serializers}.py + urls, mcp_server/envelope.py, config/views.py,
frontend/nginx.prod.conf, vite.config.ts, Dockerfile.frontend (args), deploy-labs.yml (frontend
build step), frontend/nginx.prod-kamal.conf (location inventory only).

**Skimmed (grep-targeted or partial):** ToolOutput.tsx (first 120 lines + type check),
RecipeRunner.tsx (first 80), TableDetail.tsx (annotation-save region only), KnowledgeList.tsx
(grep for pagination only), ArtifactList.tsx (grep), CreateWorkspaceModal.tsx (grep),
SqlHighlighter via imports, apps/knowledge/api/views.py (pagination constants only),
agents/tools/recipe_tool.py (tool name only), config/urls.py (route inventory).

**Not examined (honest gaps for the gap loop):**
- `apps/chat/message_converter.py` — checkpointer→UIMessage fidelity (the other half of thread
  restore; I verified neither part-type coverage nor tool-part shape on reload).
- `apps/chat/views.py` full flow & `helpers.py` (only the thread-upsert/foreign-thread region read).
- `chat/stream.py` lines 120+ (error/finish paths, tool-output emission details).
- `frontend/src/pages/DataDictionaryPage/SchemaTree.tsx`, `TableDetail.tsx` non-save regions,
  `KnowledgeForm.tsx`, `KnowledgeList.tsx`, `RecipeRunDetail.tsx`, `RecipesList.tsx`,
  `ArtifactList.tsx` full, `CreateWorkspaceModal.tsx` full, `SearchFilterBar.tsx`,
  `SqlHighlighter.tsx`, `starterQuestions.ts`, `relativeTime.ts`, `providerMeta.tsx`,
  `brandIcons.tsx`, `NavItem.tsx`, `RoleBadge.tsx`, all `components/ui/*` primitives, `index.css`.
- Frontend test suite & QA scenarios (`frontend` playwright projects, `tests/qa/`) — I did not
  assess what the e2e tests would or wouldn't have caught.
- Artifact sandbox HTML generation (`apps/artifacts/views.py`) — the producer side of the
  `artifact-query-data`/`scout-print` postMessage contracts.
- `services/export.py` (only noted as SPA-unconsumed).
- Runtime verification: no browser run; all findings are static traces. F2a's *observable*
  behavior in labs (banner vs. silent wrong-target) depends on what the labs host serves at
  `/health/`, which I could not check from here.
