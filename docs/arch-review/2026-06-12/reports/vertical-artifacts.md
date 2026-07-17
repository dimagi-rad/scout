# Vertical Review: Artifacts (end to end)

Reviewer: `vertical:artifacts`
Scope: generation tools (`create_artifact`/`update_artifact`), static-data vs live `source_queries`,
the sandbox renderer, the query-data endpoint, export (all formats), and the embed/widget SDK.
Method: read-only trace from entry points (agent tool, HTTP routes, frontend) to consequence.
All claims labeled with confidence; comments/docstrings treated as claims, verified against logic.

---

## Capability scorecard (what % actually works)

| Capability | Functional % | Verdict |
|---|---|---|
| Artifact generation (create/update tool) | ~95% | Works; `render_url` field is fictional but harmless |
| Static-data artifacts (single-tenant) | ~100% | Works end to end |
| Live `source_queries` — **single-tenant** workspace | ~95% | Works; sandbox auto-fetch + Data tab |
| Live `source_queries` — **multi-tenant** workspace | ~0% | **BROKEN**: query-data routes to one tenant schema, not the view schema |
| Sandbox renderer (react/plotly/markdown/svg/html) | ~90% | Works; sandbox isolation is weak (allow-scripts + allow-same-origin) |
| Export — HTML | ~30% | Reachable by URL only (no UI); react template is broken for the documented code style |
| Export — PNG | 0% | Returns 501; the playwright implementation is unreachable dead code |
| Export — PDF (server `/export/pdf/`) | 0% | Returns 501; real PDF is a *client-side* browser-print path, a different mechanism |
| Export — PDF (client print-to-PDF) | ~90% | Works (postMessage `scout-print` → iframe `window.print()`) |
| Embed/widget SDK (`widget.js`) | ~70% | Loads iframe, tenant select works; `setMode`/`theme` are no-ops |
| Public shared-thread artifacts | ~40% | Code shown as raw `<pre>` text, never rendered; no live data |

---

## Findings

### ART-1 — Live artifacts are broken in every multi-tenant workspace (query-data ignores the view schema)
**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace (routing) / strong-inference (breakage) · Complexity: accidental**

`ArtifactQueryDataView` resolves the execution context by grabbing an arbitrary single
tenant and loading *that tenant's* schema, regardless of how many tenants the workspace has:

- `apps/artifacts/views.py:795` — `tenant = await artifact.workspace.tenants.afirst()`
- `apps/artifacts/views.py:800` — `ctx = await load_tenant_context(tenant.external_id)`
- `apps/artifacts/views.py:822` — `result = await execute_query(ctx, sql)` (runs against `t_<id>`)

But for a multi-tenant workspace the agent is explicitly instructed that tables are
namespaced views that live only in the **view schema** (`ws_<hash>`), not in any single
tenant schema:

- `apps/agents/graph/base.py:299-303` — `_MULTI_TENANT_NAMESPACE_HINT`: "Tables are
  namespaced views prefixed with the tenant name using double underscore:
  `{tenant_name}__{table_name}`."
- The physical views are created in the view schema only: `apps/agents/graph/base.py:367-371`
  `CREATE VIEW {}.{} AS SELECT * FROM {}.{}` planned into the `ws_<hash>` schema.

The correct router exists and is used by the MCP `query` tool:
- `mcp_server/context.py:83` `load_workspace_context()` — single-tenant → `load_tenant_context`,
  multi-tenant (≥2) → `WorkspaceViewSchema` (`ws_<hash>`).
- `mcp_server/server.py:75` the `query` tool uses `load_workspace_context`.

So the agent authors `source_queries` that reference `tenantA__forms` while exploring data
through `load_workspace_context` (view schema), but the artifact's query-data endpoint executes
them through `load_tenant_context` against a single tenant schema where those views do not
exist → `relation "tenantA__forms" does not exist`. Every query in the artifact returns an
`error` entry. This is a direct contract drift between the MCP query path and the artifact
query-data path: two resolvers for the same job, only one of them tenancy-correct.

The TTL touch at `views.py:810-812` touches the *tenant* schema by `ctx.schema_name`, never
the view schema, compounding the routing mistake.

Reachable via: the artifact sandbox iframe auto-fetch (`views.py:251-274`, fires for any
artifact with `source_queries`) and the Data tab (`ArtifactPanel.tsx:57`). Any user who
opens a live artifact in a 2+-tenant workspace hits it.

Fix shape: `ArtifactQueryDataView` should call `load_workspace_context(str(workspace.id))`
(as the MCP query tool does) instead of `load_tenant_context` on the first tenant, and touch
whichever schema that resolves to.

---

### ART-2 — Server-side PNG/PDF export is unreachable dead code; the endpoint returns 501
**Status: DEBT (dead path) · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

`ArtifactExportView` advertises three formats but only HTML is implemented; PNG and PDF
short-circuit to HTTP 501 with a self-referential message:

- `apps/artifacts/views.py:980-986` — `if format in ("png", "pdf"): return JsonResponse({...
  "requires an async endpoint. Use /api/artifacts/{artifact_id}/export/{format}/ with async
  support."}, status=501)`. The suggested URL is the same one that just returned 501.

Meanwhile a full async implementation exists but is never called and cannot run:
- `apps/artifacts/services/export.py:373-453` — `export_png` / `export_pdf` use
  `from playwright.async_api import async_playwright`.
- `playwright` is **not** a declared dependency (absent from `pyproject.toml`), so these
  methods would `raise ImportError` even if something invoked them. Nothing does:
  `ArtifactExportView` only ever calls `export_html()` (`views.py:973`).

The real "Export to PDF" feature is a *different* mechanism entirely — client-side browser
print scoped to the iframe (ART-7), added later (`1402dc1`). The server export service is
vestige from an earlier design. Note also there is **no frontend caller of `/export/<format>/`
at all** (grep of `frontend/src` finds the sandbox, query-data, list, patch, delete calls but
no export call), so the entire export HTTP surface is reachable only by typing the URL.

---

### ART-3 — Exported standalone HTML cannot render the React code style the agent is told to write
**Status: BROKEN-NOW (for the only working export format) · Impact: correctness · Confidence: strong-inference · Complexity: accidental**

`export_html()` for react artifacts inlines the agent's code verbatim into a
`<script type="text/babel">` block and then renders a hardcoded `<App data={data}>`:

- `apps/artifacts/services/export.py:196-211` — `{code}` is dropped into a babel script, then
  `root.render(<App data={data} />)`.

Two problems, both contradicting the documented contract:
1. The agent is instructed to write `export default function RevenueChart({ data }) {...}`
   (`apps/agents/prompts/artifact_prompt.py`, "Component Structure" and the worked example
   that defines `RevenueChart`, not `App`). The export template references a global `App`
   that such code never defines → `App is not defined`. The live sandbox handles this with
   `stripModuleSyntax` + multi-name component discovery (`views.py:336-456`), but the export
   template has none of that logic.
2. `export default` is ES-module syntax; `@babel/standalone` with `type="text/babel"` and the
   default preset does not strip module syntax, so the code is a parse error in the standalone
   HTML. The sandbox strips it first (`stripModuleSyntax`); the export does not.

Additionally, exported HTML embeds only `artifact.data` (static) — `data_json=json.dumps(artifact.data or {})`
(`export.py:369`) — so a live-query artifact exports with **empty data** even if the React
code were fixed. The export path predates `source_queries` and was never reconciled with it.

Reachability is low (ART-2: no UI calls it), which is the only reason this is not louder.

---

### ART-4 — Sandbox isolation is weak: `allow-scripts` + `allow-same-origin` runs agent-authored JS same-origin with the app
**Status: LATENT · Impact: security · Confidence: verified-by-trace (config) / strong-inference (exploitability) · Complexity: essential-ish (tension between rendering React and isolating it)**

The artifact iframe is mounted with:
- `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx:194` —
  `sandbox="allow-scripts allow-same-origin allow-modals"`.

`allow-scripts` together with `allow-same-origin` is the documented anti-pattern: a same-origin
framed document can reach `window.frameElement` / parent and, more importantly, runs in the
**same origin** as the Scout app. The sandbox document is served from
`/api/workspaces/<id>/artifacts/<id>/sandbox/` (same origin as the SPA), its CSP allows
`'unsafe-eval'` and `connect-src 'self'` (`views.py:44-51`), and the code executed is
agent-generated React compiled at runtime via `Babel.transform` + `new Function(...)`
(`views.py:375-458`). Because the iframe is same-origin and the browser sends cookies on
`connect-src 'self'` fetches, artifact JavaScript can call authenticated Scout APIs
(`/api/workspaces/<id>/...`) as the viewing user.

The threat is realistic in this product: `source_queries` return **untrusted tenant data**,
and the agent composes component code influenced by user prompts and that data — a
prompt-injection or data-injection path can steer the agent into emitting JS that exfiltrates
via same-origin fetches. The server-side `_json_safe`/escaping protects the HTML transport but
not the executed component. The SVG *export* path is sanitized with bleach
(`export.py:140-152`), but the live sandbox react/html/svg renderers execute arbitrary code by
design.

Essential tension: rendering interactive React requires script execution. The accidental part
is `allow-same-origin`; a cross-origin sandbox host (separate origin / `srcdoc` without
same-origin) plus tightening `connect-src` away from `'self'` would remove the cookie-bearing
API reach. Flagged for the security lens to adjudicate severity.

---

### ART-5 — `render_url` returned to the agent points at a route that does not exist
**Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

Both artifact tools return `render_url = f"/artifacts/{artifact.id}/render/"`:
- `apps/agents/tools/artifact_tool.py:209` and `:322`.

No such route exists. The real render path is workspace-scoped
(`/api/workspaces/<workspace_id>/artifacts/<id>/sandbox/`, `apps/artifacts/urls.py`) and the
frontend never uses `render_url` — `ChatMessage.tsx` extracts the `artifact_id` from the tool
output and calls `openArtifact(id)` (`ChatMessage.tsx:387-397`), which sets store state and
mounts `ArtifactPanel` with the sandbox URL it builds itself (`ArtifactPanel.tsx:192`). So the
field is dead, but it is fed to the LLM as a documented return value ("render_url: URL path to
render the artifact", tool docstring) — if the model ever surfaces it to a user as a link, it
404s. Harmless today, latent confusion for the agent.

---

### ART-6 — Public shared-thread artifacts are never rendered; only raw source is shown, with no live data
**Status: DEBT (degraded feature) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The public thread page renders artifacts by dumping their source code as preformatted text,
except markdown:
- `frontend/src/pages/PublicThreadPage.tsx:171-191` — `ArtifactPreview` renders markdown via
  `<Markdown>` but every other type (react/plotly/svg/html) is shown as
  `<pre>{artifact.code}</pre>`.

The backend serializes `code` and static `data` only (`apps/chat/thread_views.py:247-256`
via `_get_thread_artifacts`); there is no sandbox iframe and no query-data fetch on the public
page, so even if it rendered, live-query artifacts would have no data. This is the public-share
surface the cartography flagged as drift: share-creation UI was removed 2026-06-04 but the
public endpoint (`/api/chat/threads/shared/<token>/`, `config/urls.py`) and this page remain
live. For a viewer, a "shared dashboard" is a code listing, not a dashboard.

---

### ART-7 — Client-side PDF export works, but is the only PDF path and is undiscoverable as such
**Status: COSMETIC / works · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

The functional PDF export is entirely client-side: the panel posts `{type: "scout-print"}` to
the sandbox iframe, which calls `window.print()` so the print job is scoped to the artifact:
- `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx:42-50` (sender)
- `apps/artifacts/views.py:662-667` (iframe listener) + print CSS at `views.py:179-212`.

This works and is the right approach. The finding is the duplication/confusion: there is also
a server `/export/pdf/` route that 501s (ART-2) and a never-called `export_pdf()` service
(ART-2). A future maintainer wiring "Export PDF" to the server route would get a 501; the real
mechanism is undocumented except in code comments. The button is also disabled on the Data tab
(`ArtifactPanel.tsx:168`) — correct, since print targets the View iframe.

---

### ART-8 — Widget SDK `setMode()` and `theme` are no-ops; only `setTenant` is wired
**Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

`widget.js` exposes `setMode` and forwards a `theme` URL param, but neither has a live effect:
- `frontend/public/widget.js:145-147` — `setMode` posts `scout:set-mode`.
- `frontend/src/pages/EmbedPage.tsx:66-68` — the handler for `scout:set-mode` only
  `console.log`s; it does not change mode.
- Embed `mode` is read once from the URL via `useEmbedParams` (memoized with `[]` deps,
  `hooks/useEmbedParams.ts:15-27`) and drives sidebar/artifact visibility in `EmbedLayout`
  (`EmbedLayout.tsx:11-13`). A runtime `setMode` cannot retroactively change it.
- `theme` is parsed (`useEmbedParams`) but has no consumer (grep finds no theme application
  for embed). Dead param.

`setTenant` does work (`EmbedPage.tsx:60-65` → `ensureTenant`). The widget version string is
`"0.3.0-popup-fix"` (`widget.js:4`) — a hand-bumped ad-hoc tag, not synced to any package
version.

---

## What's fine

- **Single-tenant live query execution** — `ArtifactQueryDataView` correctly executes each
  named query, isolates failures per-query (`views.py:814-841`), coerces non-JSON types
  (`_json_safe`, `views.py:749-761`), enforces read-only role + search_path + statement_timeout
  via the shared `execute_query`/`_execute_async` path (`mcp_server/services/query.py:38-65`),
  and resets the TTL on the (single-tenant) schema. The frontend `mergeQueryResults`
  (`views.py:279-296`) keys rows by query name and always yields arrays — matching the prompt
  contract.
- **AuthZ on artifact routes** — every view resolves the workspace through
  `resolve_workspace` / `aresolve_workspace`, which require a `WorkspaceMembership`
  (`workspace_resolver.py`), and scope the artifact lookup by `workspace=workspace`. Cross-
  workspace access returns 404 (test `test_non_member_returns_404`). Unauthenticated returns
  401. Reachability of artifacts is gated by membership, not role — consistent with the rest
  of the app (role enforcement is a separate, known-weak area; not artifact-specific).
- **CSP + nonce on the sandbox HTML** — per-request nonce (`secrets.token_urlsafe`),
  `default-src 'none'`, `</script>` escaping of injected JSON (`views.py:707`), `nosniff`,
  `X-Frame-Options: SAMEORIGIN`. The transport hardening is solid; the residual risk is the
  executed component (ART-4), not the injection of the data blob.
- **SVG export sanitization** — `sanitize_svg` via bleach with an explicit tag/attr allowlist
  (`export.py:23-152`); applied in `export_html` for svg.
- **Soft delete + versioning** — `SoftDeleteManager`, `all_objects` for undelete,
  `create_new_version`/`get_version_history` with cycle guard (`models.py:199-222`).
- **Artifact panel state on workspace switch** — `selectDomain`/thread setters reset
  `activeArtifactId` to null (`store/uiSlice.ts:46-54`), so the cross-workspace threadId class
  of bug does not appear to recur for the artifact panel.
- **Embed framing** — `EmbedFrameOptionsMiddleware` (`config/middleware/embed.py`) and the
  nginx kamal config both replace `X-Frame-Options` with `frame-ancestors` for `/embed/`,
  gated on `EMBED_ALLOWED_ORIGINS`; cookie SameSite=None handling is documented and
  conditioned on the same flag.

---

## Coverage log

### Deep-read (line by line)
- `apps/artifacts/views.py` (all 988 lines: sandbox template, renderer JS, query-data, list/detail/undelete/export)
- `apps/artifacts/services/export.py` (all)
- `apps/artifacts/models.py` (all)
- `apps/artifacts/urls.py` (all)
- `apps/agents/tools/artifact_tool.py` (all)
- `apps/agents/prompts/artifact_prompt.py` (all)
- `mcp_server/context.py` (all — `load_tenant_context` vs `load_workspace_context`)
- `mcp_server/services/query.py` (all)
- `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx` (all)
- `frontend/src/pages/ArtifactsPage/{ArtifactsPage,ArtifactList}.tsx` (all)
- `frontend/src/store/artifactSlice.ts` (read top half; actions verified)
- `frontend/src/store/uiSlice.ts` (artifact state portions)
- `frontend/public/widget.js` (all)
- `frontend/src/pages/EmbedPage.tsx`, `components/EmbedLayout/EmbedLayout.tsx`, `hooks/useEmbedParams.ts` (all)
- `frontend/src/pages/PublicThreadPage.tsx` (all relevant: artifact rendering)
- `config/views.py` (widget_js_view), `config/urls.py`, `config/middleware/embed.py`
- `apps/artifacts/tests/test_artifact_query_data.py` (all)
- relevant slices of `apps/agents/graph/base.py` (lines 230-369: schema-context + view creation)

### Skimmed
- `apps/workspaces/services/schema_manager.py` (only `build_view_schema` / view-name section, grep-level)
- `frontend/src/components/ChatMessage/ChatMessage.tsx` (artifact-tool handling only, lines 70-110, 340-420)
- nginx prod configs (embed/widget/api locations only, grep-level)
- production/connectlabs settings (frame/cookie portions only)
- git history of artifact files (log only)

### NOT examined (in scope, left for gap loop)
- The full sandbox renderer behavior for `plotly`/`html`/`svg`/`markdown` types at runtime —
  I read the JS but did not execute or browser-verify any artifact rendering.
- `useEmbedMessaging.ts` and `useAutoResize.ts` internals (only their call sites read) — the
  postMessage origin checks and resize loop were not line-traced.
- The `_get_thread_artifacts` / `_load_thread_messages` implementations in `chat/thread_views.py`
  (only the public view entry was read; how artifacts are associated to a thread was not traced —
  ART-6 assumes code+static-data serialization from the response shape).
- Whether `conversation_id` (thread linkage) is reliably set on agent-created artifacts and how
  artifacts are joined back to a thread for the share page.
- Babel/CDN supply-chain posture beyond noting no SRI on the CDN `<script>` tags (sandbox loads
  React/Babel/Recharts/Plotly/D3 from jsdelivr/unpkg/cdnjs without integrity hashes) — flagged
  but not developed into a finding; defer to ops/security lens.
- Artifact admin (`apps/artifacts/admin.py`) — not opened.
- Performance of query-data when an artifact has many `source_queries` (sequential `await
  execute_query` in a loop, each opening a new AsyncConnection — `views.py:815-822`,
  `query.py:41`); noted as a possible cost-perf issue but not measured.
- `update_artifact` concurrency / version race (two updates creating sibling versions off the
  same parent) — not analyzed.
