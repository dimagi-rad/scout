# Lens Report — AuthZ & Security Surface

*Reviewer: cross-cutting lens "AuthZ & security surface". Defensive audit of Scout's
own production codebase, report-only. Branch `main`, HEAD `35e4230`.*

This report enumerates every reachable endpoint/tool, the check that guards it, and the
gaps. Confidence labels per the methodology: `verified-by-trace` / `strong-inference` /
`hypothesis`. Comments/docstrings treated as claims, verified against logic.

---

## 1. Trust-boundary model (as built)

```
Browser (session cookie + CSRF) ─► Django API ─► MCP server (FastMCP, no per-call authz)
                                       │              └─ psycopg as MANAGED_DATABASE_URL **superuser**
                                       │                 then SET ROLE {schema}_ro for the `query` tool
                                       ├─► procrastinate worker (materialize, dbt as superuser)
                                       └─► allauth OAuth (Google/GitHub/CommCare/Connect/OCS)
```

Two authentication planes exist:
- **HTTP API**: Django session cookies, DRF `SessionAuthentication` + `IsAuthenticated`
  default (`config/settings/base.py:256-271`); raw async views use `@async_login_required`
  / `@login_required_json` (`apps/users/decorators.py`). CSRF enforced via middleware;
  the chat view additionally `@csrf_protect` (`apps/chat/views.py:62`).
- **MCP plane**: **no authentication and no authorization at the tool layer.** The MCP
  server trusts that the Django agent graph injected the correct `workspace_id`/`user_id`.
  DNS-rebinding protection + a host allowlist restrict network reach
  (`mcp_server/server.py:909-912`), and `auth.py` only *extracts* OAuth tokens from
  request `_meta` — it never verifies a caller.

The whole MCP authz story rests on one mechanism: `apps/agents/graph/base.py`
`_make_injecting_tool_node` (line 439-477) overwrites every MCP tool call's
`workspace_id`/`user_id`/`thread_id` with values taken from agent **state**, and
`_llm_tool_schemas` (396-436) hides those params from the LLM so it cannot supply them.
The state values originate in `apps/chat/views.py:209-215`, derived from the
authenticated user's verified `WorkspaceMembership`. **This is sound for the chat path**
(verified-by-trace) — the LLM cannot inject a foreign `workspace_id`.

---

## 2. Endpoint inventory and guards

Legend for "Role enforced": `member` = any `WorkspaceMembership`; `RW/MANAGE` /
`MANAGE` = role checked; `owner` = scoped to `user=request.user`; `none` = auth only.

### Auth (`/api/auth/…`, `apps/users/auth_views.py`, `views.py`)

| Endpoint | Guard | Notes |
|---|---|---|
| `GET csrf/` | none (sets cookie) | ok |
| `GET me/` | `@async_login_required` | ok |
| `POST login/` | rate-limited (`check_rate_limit`) | per-email throttle, MD5-ish locmem |
| `POST signup/` | rate-limited + password validators | ok |
| `POST logout/` | none | ok |
| `*/disconnect/` | `@login_required_json`, `user`-scoped | ok |
| `GET providers/` | public; richer if authed | ok |
| `tenants/`, `tenants/select/`, `tenants/ensure/` | `@async_login_required`, `user`-scoped | `ensure` verifies Connect access via live API (`views.py:392-398`) |
| `connections/`, `connections/<id>/` | `@async_login_required`, `user`-scoped | API-key CRUD; OCS team auto-detect |
| `api-key-providers/` | `@async_login_required` | static metadata |

All `user`-scoped. **Verified-by-trace**: no IDOR — every query filters `user=user`.

### Workspaces (`apps/workspaces/api/workspace_views.py`)

| Endpoint | Role enforced |
|---|---|
| `GET /api/workspaces/` | member (lists own memberships) |
| `POST /api/workspaces/` | authed; validates tenant access (`:172-184`) |
| `GET <id>/` | member |
| `PATCH <id>/` (rename, system_prompt) | **MANAGE** (`:302`) |
| `DELETE <id>/` | **MANAGE** (`:333`) + last-workspace guard |
| `GET members/` | member |
| `POST members/` | **MANAGE** (`:390`); target must share a tenant (`:411-419`) |
| `PATCH/DELETE members/<id>/` | **MANAGE** (self-removal allowed); last-manager guard |
| `GET/POST/DELETE tenants/` | GET member; POST/DELETE **MANAGE** + tenant-access check |

**Role enforcement on workspace management is correct and consistent** (verified-by-trace).
This contradicts the seed "roles ~unenforced": the *management* surface enforces MANAGE.
The dead code is `apps/workspaces/permissions.py` (the three `BasePermission` classes),
which are imported nowhere — confirmed (see Finding S-7). Enforcement is hand-rolled inline.

### Content endpoints (the gap)

| Endpoint | Role enforced | Mutating? |
|---|---|---|
| `POST /api/chat/` (stream) | member (`chat/views.py:109-114`) | yes — drives agent (materialize, teardown, write artifacts/knowledge/recipes) |
| `GET threads/`, `…/messages/`, `…/share/`, `…/viewed/` | owner (`thread__user`) | share PATCH = owner |
| `artifacts/` list/data/sandbox/query-data/export | member | — |
| `PATCH/DELETE artifacts/<id>/`, `undelete/` | member | **yes** |
| `knowledge/` list/create/detail/PUT/DELETE/import/export | member | **yes** (feeds system prompt) |
| `recipes/` list/detail/PUT/DELETE/run | member | **yes** |
| `data-dictionary/` GET | member | — |
| `data-dictionary/tables/<n>/` PUT (annotate) | **READ blocked** (`api/views.py:500`) | yes |
| `refresh/` | **RW/MANAGE** (`api/views.py:330`) | yes |
| `materialization/cancel/`, `materialize/retry/` | member, **own threads only** | yes |
| `jobs/active/`, `jobs/<id>/cancel/` | member, **own threads only** | yes |

**Finding S-1** lives here: most content-mutating endpoints enforce only *membership*,
not role, so a `READ`-role member can mutate shared workspace state.

### MCP tools (`mcp_server/server.py`)

| Tool | In-tool authz |
|---|---|
| `list_tables`, `describe_table`, `get_metadata`, `query`, `get_lineage`, `get_schema_status`, `teardown_schema` | **none** — resolves any `workspace_id` |
| `run_materialization` | checks tenant membership + thread ownership (`:553-570`) |
| `get_materialization_status`, `cancel_materialization` | by `run_id`, **no workspace/user check** |
| `list_pipelines` | none (static) |

See Finding S-2 (MCP confused-deputy posture) and S-6 (`cancel_materialization` IDOR-by-id).

### Public / unauthenticated

| Endpoint | Guard |
|---|---|
| `GET /api/chat/threads/shared/<token>/` | `is_shared=True` + token (`thread_views.py:227`) |
| `GET /api/recipes/runs/shared/<token>/` | `is_public=True` + token; `AllowAny`, `authentication_classes=[]` |
| `GET /widget.js` | public static, `Access-Control-Allow-Origin: *` |
| `GET /` , `/health/` | public |

Share tokens are unguessable (`secrets`/UUID); exposure is scoped to the single shared
object. See Finding S-8 (share surface drift — still creatable via API though UI removed).

### Transformations (`apps/transformations/`) — the only DRF-router surface

| Endpoint | Guard |
|---|---|
| `assets/` CRUD | member of tenant / **RW-MANAGE** of workspace (`views.py:74-87`); SYSTEM read-only |
| `runs/` (read) | tenant/workspace membership |
| `runs/trigger/` POST | tenant membership (`views.py:137`) |

This surface is the most dangerous: see **Finding S-3** (arbitrary SQL as DB superuser).

---

## 3. Findings

### S-3 — Transformation assets execute arbitrary SQL as the managed-DB **superuser**, no validation, no role downgrade, no schema confinement  ·  status BROKEN-NOW · impact security (privilege escalation + cross-tenant) · confidence verified-by-trace

**Chain (entry → consequence):**
1. `apps/transformations/serializers.py:6-23` — `TransformationAssetSerializer` exposes
   `sql_content` as a **writable** field.
2. `apps/transformations/views.py:54-87` — a user who is a member of a tenant (for
   `scope=tenant`) or has `read_write`/`manage` on a workspace (for `scope=workspace`)
   may `POST /api/transformations/assets/` with arbitrary `sql_content`. SYSTEM scope is
   blocked, tenant/workspace are not.
3. `apps/transformations/views.py:121-166` — `runs/trigger/` requires only
   `request.user.tenant_memberships.filter(tenant=tenant).exists()` and calls
   `run_transformation_pipeline(tenant, schema_name, workspace)`.
4. `apps/transformations/services/executor.py:121-149` — writes the asset SQL into an
   ephemeral dbt project and runs it via `run_dbt`, using
   `generate_profiles_yml(... db_url=settings.MANAGED_DATABASE_URL)`.
5. `mcp_server/services/dbt_runner.py:28-61` — the profile connects as the
   **`MANAGED_DATABASE_URL` user**, which is the same role that runs `CREATE SCHEMA` /
   `CREATE ROLE` in `schema_manager.py` (i.e. a PostgreSQL superuser/owner). There is **no
   `SET ROLE`, no `SET search_path` lockdown, and no SQL validation** — contrast the
   `query` tool which does all three (`mcp_server/services/query.py:44-49`).

**Consequence:** any tenant member can author SQL that runs with full superuser
privileges against the managed database — read or write **any tenant's schema**, `COPY`,
`DROP`, create roles, or read `pg_read_file`-class functions (none of the
`sql_validator.DANGEROUS_FUNCTIONS` blocks apply here). This is a complete bypass of the
read-only/tenant-isolation guarantees the `query` path is built to enforce, reachable by
an ordinary authenticated tenant member.

**Reachable via:** `POST /api/transformations/assets/` then `POST
/api/transformations/runs/trigger/` (router registered at `config/urls.py:96` →
`apps/transformations/urls.py`).

**Essential vs accidental:** accidental. dbt legitimately needs DDL to materialize models,
but running tenant/workspace-authored SQL as the cluster superuser with no confinement is
not essential — a per-tenant role with `SET ROLE` + restricted search_path (the very thing
TODO.md line 39 lists as undone) would contain it.

*Note:* the sibling transformations vertical reviewer independently flagged the
superuser/no-downgrade execution model; this finding adds the authz reachability chain
(who can reach it and with what role).

---

### S-1 — Workspace `READ` role does not gate content mutation; READ members can drive the agent, edit knowledge, run recipes, delete artifacts  ·  status BROKEN-NOW · impact correctness/security (privilege within tenant) · confidence verified-by-trace

`WorkspaceRole` defines READ / READ_WRITE / MANAGE. Management endpoints enforce it
(§2), but the content surface enforces only membership:

- **Knowledge** (`apps/knowledge/api/views.py`): `POST/PUT/DELETE/import` call only
  `resolve_workspace_drf` (membership), never check role (`:115, :189, :212, :271`). A
  READ member can create/edit/import `KnowledgeEntry` rows, which
  `apps/agents/graph/base.py:730-733` injects verbatim into the **system prompt** of every
  other member's agent (`KnowledgeRetriever.retrieve()`). This is a stored
  prompt-injection vector available to the lowest role.
- **Recipes** (`apps/recipes/api/views.py`): `PUT/DELETE/run` check membership only
  (`:66, :76, :89`). `prompt_template` is agent-executed.
- **Artifacts** (`apps/artifacts/views.py`): `PATCH/DELETE/undelete` check membership only
  (`:893, :915, :926`).
- **Chat** (`apps/chat/views.py:109-114`): any member may stream. Through the agent a READ
  member can trigger `run_materialization` and `teardown_schema` (destructive — drops all
  materialized data) since those MCP tools have no role concept.

**Reachable via:** every content route under `/api/workspaces/<id>/…` and `/api/chat/`.

**Essential vs accidental:** accidental — the role taxonomy exists and is enforced
elsewhere; the content endpoints simply never consult it. Compare `RefreshSchemaView`
(`api/views.py:330`) and `TableDetailView.put` (`:500`), which *do* gate on role, proving
the inconsistency is an omission, not a design choice.

**Caveat:** impact is bounded to within a single workspace's own members and data; there
is no cross-tenant escalation here (that is S-3). Severity is "you handed a read-only
collaborator write/destroy power", not "external compromise".

---

### S-2 — MCP tools are a confused deputy: no per-call authz; isolation depends entirely on the Django graph injecting the right `workspace_id`  ·  status LATENT · impact security · confidence verified-by-trace

`mcp_server/server.py` tools `list_tables`/`describe_table`/`get_metadata`/`query`/
`get_lineage`/`get_schema_status`/`teardown_schema` accept `workspace_id: str = ""` and
resolve context against it (`_resolve_mcp_context` → `load_workspace_context`) **with no
check that the caller is entitled to that workspace.** Only `run_materialization` verifies
membership (`server.py:553-570`).

Today the only client is the Django agent graph, which injects a verified `workspace_id`
(§1), so this is **not currently exploitable through the product UI** (LATENT, not
BROKEN-NOW). The risk is structural: the MCP server listens on HTTP (`streamable-http`,
port 8100) and its sole defense against a direct caller is network reach
(`TransportSecuritySettings` host allowlist, `server.py:909-912`) — *not* authentication.
Anything that can reach the MCP port (a co-located process, an SSRF in another service, a
future second client, the dbt runner network) can call `teardown_schema(confirm=True,
workspace_id=<any>)` and destroy any workspace's data, or `query` any schema, with no
credential. `teardown_schema`'s only guard is the `confirm` flag, which is not a security
control.

**Essential vs accidental:** accidental. The cartography's own note ("MCP authz: trust the
caller") is accurate. A signed/scoped capability in `_meta` (the channel already used for
OAuth tokens) verified per call would close it. TODO.md line 40 (append-only audit) is the
adjacent gap — there is currently **no durable record** of who invoked which tool
(`envelope.py:107` logs to a Python logger only).

---

### S-6 — `cancel_materialization` and `get_materialization_status` MCP tools accept any `run_id` with no workspace/user scoping  ·  status LATENT · impact correctness · confidence verified-by-trace

`mcp_server/server.py:407-493` — both tools look up `MaterializationRun` by `id` alone and
return/mutate it. `cancel_materialization` flips an arbitrary in-progress run to FAILED.
Because the LLM cannot supply `run_id` for another workspace under normal flow (it only
learns run_ids from its own `run_materialization` results), this is LATENT, but it is a
missing scope check at the same boundary as S-2: a direct MCP caller (or a prompt that
smuggles a foreign run_id into the conversation) could cancel another tenant's
materialization. The HTTP-side equivalents (`materialization_cancel_view`,
`cancel_job_view`) *are* correctly scoped to `thread__user=user`
(`api/materialization_views.py:63-70`, `api/jobs_views.py:177-181`) — so the contract is
enforced on one side of the seam and not the other.

---

### S-4 — Artifact sandbox executes agent-authored code with `unsafe-eval` and an HTML path that runs arbitrary `<script>`; live-query path assumes single-tenant  ·  status LATENT/DEBT · impact security (contained) · confidence verified-by-trace

`apps/artifacts/views.py:34-51` — the sandbox CSP is `default-src 'none'` with
`script-src 'nonce-…' 'unsafe-eval' <several CDNs>`. Agent-generated React/Plotly/SVG is
transpiled and run via `new Function(...)` (`:382`, `:563`, `:605`), and the `html`
artifact type re-injects and executes any `<script>` it contains (`renderHTML`, `:498-523`)
with **no sanitization** (only `svg` is bleach-sanitized, and only on the *export* path,
`services/export.py:140-152`). This is intentional (the artifact is a sandboxed iframe,
`X-Frame-Options: SAMEORIGIN`, served same-origin so it can call `query-data`). The
exposure is therefore: agent-authored JS runs with the **authenticated user's same-origin
cookies** inside the iframe. Since the agent is itself constrained and the artifact is
scoped to the user's own workspace, this is contained — but the `'unsafe-eval'` + arbitrary
`<script>` combination means a successful prompt-injection that gets the agent to emit a
malicious artifact runs in the victim's authenticated origin. DEBT-class hardening:
restrict `html` artifacts or strip scripts; drop `'unsafe-eval'` if the Babel path allows.

Secondary, **correctness**: `ArtifactQueryDataView` (`views.py:773-843`) resolves the
tenant via `artifact.workspace.tenants.afirst()` — the *first* tenant only. For a
multi-tenant workspace the live queries run against one tenant's schema (or the wrong one),
not the `ws_*` view schema the SQL was authored against. Strong-inference: multi-tenant
live artifacts return errors or wrong data. (Not a security hole — it under-exposes.)

The PNG/PDF export (`services/export.py:373-453`) loads agent HTML into a server-side
headless Chromium with `set_content` and **no CSP**; the page can fetch arbitrary URLs
(SSRF from the export worker). The HTML is agent-authored within the same workspace, so the
trust is the same as S-4's; still worth noting the export browser has no egress restriction
(mirrors TODO.md line 41 for loaders).

---

### S-5 — OAuth domain allowlist is bypassed by any login that returns no email  ·  status LATENT · impact security · confidence verified-by-trace

`apps/users/adapters.py:74-104` (`pre_social_login`) enforces
`SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` (default `{"commcare": ["dimagi.com"]}`). But
`:88-90` returns (allows) when the login carries **no email** — documented as best-effort
for providers like Connect that don't return one. Consequence: the `commcare` provider is
nominally restricted to `@dimagi.com`, yet a CommCare OAuth response with an empty email
would pass the gate. CommCare does return email in practice, so this is LATENT
(strong-inference that it's not currently exploited), but the allowlist is not a hard
control — it is an allow-by-default-on-missing-data filter. Pair with
`SOCIALACCOUNT_EMAIL_REQUIRED = False` and `SOCIALACCOUNT_EMAIL_VERIFICATION = "none"`
(`settings/base.py:207, 214`): emails from these providers are *trusted as verified* without
Scout verifying them (`SOCIALACCOUNT_EMAIL_AUTHENTICATION = True`, `:212`), which is the
linchpin of the auto-merge path. That merge path is itself correctly fail-closed (next).

---

### S-9 — Knowledge import has no zip-bomb / size guard; system_prompt + knowledge are unbounded agent-context injection by low-privilege members  ·  status DEBT · impact cost-perf / security · confidence strong-inference

`apps/knowledge/api/views.py:283-307` reads every `.md` member of an uploaded zip with
`zf.read(name).decode("utf-8")` and no decompressed-size cap — a classic decompression
bomb (DoS). The route is `IsAuthenticated` + membership only (no role), so any member can
submit. Combined with S-1, a READ member can also push large `KnowledgeEntry` content that
inflates every agent system prompt for the workspace (cost-perf on every chat turn; the
6 000-char schema budget in `graph/base.py:81` does not bound the knowledge section).

---

## 4. What's verified fine

- **Chat `workspace_id`/`user_id` injection is non-bypassable by the LLM.** `_llm_tool_schemas`
  hides the params; `_make_injecting_tool_node` overwrites them from server-derived state
  (`graph/base.py:396-477`). Verified-by-trace.
- **Thread ownership is enforced everywhere.** `chat_view` rejects POSTs to foreign threads
  with 404 (`views.py:121-137`); `run_materialization` and `materialize/retry` both
  re-validate `Thread(id, user, workspace)` before binding a ThreadJob
  (`server.py:563-570`, `materialization_views.py:159-166`). Cancel/jobs scoped to
  `thread__user=user`.
- **Per-tenant credential resolution is fail-closed.** `aresolve_credential`
  (`credential_resolver.py:66-95`) refuses an OAuth token whose live OIDC `team` claim no
  longer matches the membership's `team_slug` (`_oauth_team_mismatch`). Materialization
  resolves credentials **per TenantMembership** (`tasks.py:264`), so no cross-tenant token
  reuse. Verified-by-trace.
- **Auto-merge is fail-closed on email verification.** `reconcile_existing_user_on_login`
  (`signals.py:104-116`) refuses to merge unless the canonical user holds a `verified=True`
  `EmailAddress` for the incoming email. This is the documented secure-by-design refusal.
- **OAuth tokens and API keys are encrypted at rest** with Fernet via
  `EncryptingSocialAccountAdapter` (`adapters.py:54-72`) and `encrypt_credential`. Audit
  scrubs `oauth_tokens` from logs (`envelope.py:82-87`).
- **SQL validator is solid for the `query` tool**: SELECT-only, single-statement,
  40+ dangerous functions blocked, LIMIT injected/capped, foreign explicit-schema access
  rejected, identifiers always passed through `psycopg.sql.Identifier`
  (`query.py:44-49`, `schema_manager.py` throughout). Tenant isolation on the `query` path
  holds via the per-schema `{schema}_ro` role's grants (USAGE only on its own schema), not
  the validator — and that role grant is correct.
- **Production settings are hardened**: `SECURE_HSTS_SECONDS=31536000` + preload,
  `SESSION/CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`, `X_FRAME_OPTIONS=DENY`,
  `SECURE_CONTENT_TYPE_NOSNIFF` (`settings/production.py`). CSRF cookie is non-HttpOnly by
  design (SPA reads it); session cookie stays HttpOnly.
- **Workspace management role enforcement** (rename/delete/members/tenants) is consistent
  and correct (§2), including last-manager and last-tenant-workspace guards.
- **Schema/identifier construction is injection-safe**: every DDL path uses
  `psycopg.sql.Identifier` / parameterized queries; `_parse_db_url` re-validates the schema
  against `^[a-z][a-z0-9_]*$` before string-embedding into the `options` param
  (`context.py:147-159`).

---

## 5. Cross-cutting patterns

1. **Authz is enforced on one side of a seam and skipped on the other.** HTTP cancel/jobs
   scope to `thread__user`; the MCP twins (`cancel_materialization`,
   `get_materialization_status`) do not (S-6). HTTP management enforces role; HTTP content
   does not (S-1). The `query` tool downgrades to a read-only role; the dbt path does not
   (S-3). The pattern: a control exists, proving it was understood, but its sibling site
   was missed.
2. **"Trust the caller" at the MCP boundary** (S-2) is the root posture that makes S-3/S-6
   reachable in any non-UI path. TODO.md's three unchecked security items (per-tenant role
   isolation, append-only audit, loader egress) are exactly the controls that would harden
   this boundary.
3. **Allow-by-default-on-missing-data** appears in two security gates: the email-domain
   allowlist allows empty-email logins (S-5), and the `query` validator only blocks
   *explicitly* schema-qualified foreign access (isolation is delegated to the DB role).

---

## 6. Prioritized recommendations

1. **(S-3, highest)** Run dbt/transformations under a per-tenant non-superuser role with
   `SET ROLE` + restricted search_path, OR validate `sql_content` and forbid cross-schema
   references; at minimum gate `runs/trigger` and asset creation behind MANAGE and treat
   tenant-scoped asset SQL as privileged. Unblocks: closes the only ordinary-user path to
   superuser SQL.
2. **(S-2/S-6)** Add a per-call capability check at the MCP boundary: verify a
   signed/scoped token in `_meta`, or have tools re-derive entitlement. Move `teardown` and
   `cancel` behind it. Unblocks: removes the confused-deputy class.
3. **(S-1)** Thread workspace role through content endpoints: require RW for
   knowledge/recipe/artifact mutation and for agent-driven `teardown_schema`. Reuse the
   inline pattern already in `RefreshSchemaView`. Unblocks: makes the READ role mean
   read-only.
4. **(S-9)** Cap decompressed size in knowledge import; bound the knowledge section of the
   system prompt.
5. **(S-2)** Implement the append-only `MCPAuditLog` (TODO.md line 40) so the next incident
   is forensically reconstructable.
6. **(S-5)** Reject no-email logins for providers that have a non-empty domain allowlist.

---

## 7. Coverage log

**Deep-read (line-by-line):** `config/urls.py`, `config/views.py`, `config/middleware/embed.py`,
`config/settings/base.py`, `config/settings/production.py`, `apps/workspaces/permissions.py`,
`apps/workspaces/workspace_resolver.py`, `apps/workspaces/api/workspace_views.py`,
`apps/workspaces/api/views.py`, `apps/workspaces/api/materialization_views.py`,
`apps/workspaces/api/jobs_views.py`, `apps/workspaces/api/jobs_cancel.py`,
`apps/workspaces/services/schema_manager.py`, `mcp_server/server.py`, `mcp_server/auth.py`,
`mcp_server/context.py`, `mcp_server/services/query.py`, `mcp_server/services/sql_validator.py`,
`mcp_server/envelope.py`, `mcp_server/services/dbt_runner.py`, `apps/agents/graph/base.py`,
`apps/agents/mcp_client.py`, `apps/chat/views.py`, `apps/chat/helpers.py`,
`apps/chat/thread_views.py`, `apps/chat/rate_limiting.py`, `apps/chat/urls.py`,
`apps/users/views.py`, `apps/users/auth_views.py`, `apps/users/decorators.py`,
`apps/users/adapters.py`, `apps/users/signals.py`, `apps/users/services/credential_resolver.py`,
`apps/users/services/merge.py`, `apps/users/services/ocs_team.py`, `apps/artifacts/views.py`,
`apps/artifacts/services/export.py`, `apps/knowledge/api/views.py`,
`apps/recipes/api/views.py`, `apps/recipes/api/serializers.py`,
`apps/recipes/services/runner.py`, `apps/transformations/views.py`,
`apps/transformations/urls.py`, `apps/transformations/serializers.py`,
`apps/transformations/services/executor.py`, `frontend/public/widget.js`.

**Skimmed / partial:** `apps/workspaces/tasks.py` (read materialize entry + refresh path +
header; janitor/resume internals not security-traced), `apps/transformations/services/commcare_staging.py`
(SQL-escaping helpers only), `apps/agents/memory/checkpointer.py` (one caller line),
`apps/artifacts/models.py` (grep for workspace/conversation_id), `apps/workspaces/models.py`
(grep for Workspace properties only).

**NOT examined (in-scope, drives gap loop):**
- `apps/agents/tools/artifact_tool.py`, `learning_tool.py`, `recipe_tool.py` — the
  agent-native tools that write platform rows directly (no MCP hop). I confirmed they take
  `workspace`/`user` from the graph builder but did not read their bodies for IDOR on
  `update_artifact`/`save_learning` (e.g. does `update_artifact(artifact_id=…)` re-check the
  artifact's workspace?). **Recommend a targeted pass.**
- `apps/users/services/tenant_resolution.py` and `services/api_key_providers/*` — provider
  payload → membership creation; the authorization-relevant question (can a crafted provider
  payload mint a membership for a tenant the user shouldn't have?) was not traced.
- `apps/users/services/token_refresh.py` — OAuth refresh (external call) not read.
- Frontend route guards / `BASE_PATH` / `/embed` SPA route — I confirmed `/embed/` is not in
  Django urls and the embed middleware/widget reference it, but did not confirm where (if
  anywhere) `/embed` is served or whether it leaks an unauthenticated chat surface. **The
  embed surface is the highest-value unexamined area.**
- `mcp_server/services/materializer.py` (1 972 LOC) and the 19 loaders — per-provider writer
  authz/SSRF (loader egress) only skimmed via the executor; not security-read.
- Django admin (`/admin/`) and per-app `admin.py` — registration confirmed, access model
  (is_staff) not audited.
- `apps/transformations/services/dbt_project.py` (how asset SQL becomes a model file — Jinja
  injection surface) not read.
- The `query-data` / sandbox `postMessage` origin checks were read in `views.py` but the
  frontend `ArtifactPanel`/iframe host wiring was not, so the parent↔iframe trust was only
  half-traced.
- DRF throttle effectiveness under multi-worker LocMemCache (noted in settings) not
  load-reasoned.

**Confidence in this report's completeness:** medium-high for the HTTP API authz matrix and
the MCP tool boundary (the core mandate); medium for OAuth/provider payload trust; low for
the embed/public surface and the agent-native tools, which are the named gaps above.
