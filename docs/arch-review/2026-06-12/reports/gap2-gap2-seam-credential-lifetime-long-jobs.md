# Gap round 2 — Credential validity through long-running background work

*Reviewer mandate: trace the "resolved once at task start, never refreshed" pattern through
materialization; establish provider token TTLs; map the exact mid-run-expiry failure surface;
check resume paths for stale-token reuse; determine token_refresh.py reachability; assess the
CommCare 429 interaction; conclude with the probability of the production scenario "multi-hour
first materialization on an OAuth-backed connection".*

*Date: 2026-06-12. Repo HEAD: 35e4230.*

---

## Executive answer

The pattern is real and fully traced. **A materialization run uses exactly one credential
snapshot for its entire lifetime.** `aresolve_credential` is called once per tenant at the top of
the worker task (`apps/workspaces/tasks.py:160`, `:264`); the returned `{"type", "value"}` dict is
passed by value into `run_pipeline` on an `asyncio.to_thread` thread, and every loader bakes the
token string into `requests.Session` headers at construction
(`connect_base.py:127`, `commcare_base.py:47`, `ocs_base.py:40-43`). There is **no re-resolution,
no proactive mid-run refresh, and no reactive refresh on 401** anywhere in the worker or MCP
process — the 401 handler in every loader raises a provider `AuthError` that nothing catches
specifically.

Provider access-token TTLs (verified against upstream source):

| Provider | TTL | Evidence |
|---|---|---|
| CommCare HQ | **15 minutes** ("expire the token every 15 minutes to match HIPAA constraints") | `dimagi/commcare-hq` `settings.py:885-889` (master, fetched 2026-06-12) |
| CommCare Connect | **2 weeks** (1,209,600 s) | `~/Code/dimagi/commcare-connect/config/settings/base.py:380-381` |
| OCS | **10 hours** (django-oauth-toolkit default 36,000 s; no override present) | `~/Code/dimagi/open-chat-studio/config/settings.py:948-960` — `OAUTH2_PROVIDER` block sets no `ACCESS_TOKEN_EXPIRE_SECONDS` |

The proactive refresh in `_aresolve_oauth_credential` fires only when the token is within
**5 minutes** of expiry at resolution time (`token_refresh.py:19`, `credential_resolver.py:102`).
Consequence: even a freshly refreshed CommCare HQ token buys at most ~15 minutes of load time.

**Bottom line on the mandated scenario:** a multi-hour first materialization on a
**CommCare-HQ OAuth** connection is not merely likely to fail — it is **structurally impossible
to complete**, on every attempt, because (a) the token dies ≤15 minutes in, (b) CommCare sources
are non-resumable so every retry restarts from zero with another ≤15-minute token, and (c) no
mid-run refresh exists. For **OCS OAuth** the same mechanism is latent (runs must outlast the
remaining fraction of a 10-hour token — plausible for large teams given the per-session detail
fetch pattern). For **Connect OAuth** it is a low per-run tail risk (2-week TTL), mitigated
further by Connect's cursor resume. This exact failure class already bit once in deployment:
commit `d4dee2e` (2026-03-26, #112) — "MCP run_materialization never refreshed expired OAuth
tokens before use, causing AUTH_TOKEN_EXPIRED errors" — was fixed **only at resolution time**;
the mid-run window was never addressed, and the `AUTH_TOKEN_EXPIRED` envelope code that fix
introduced is now producer-less residue (`mcp_server/envelope.py:31`).

---

## 1. The credential pipeline, hop by hop

### 1.1 Resolution (once, worker task start)

- `materialize_workspace` (`apps/workspaces/tasks.py:203`) iterates memberships; **per tenant**,
  inside the loop: `credential = await aresolve_credential(tm)` (`tasks.py:264`). (The legacy
  refresh path does the same once at `tasks.py:160`.)
- `aresolve_credential` (`apps/users/services/credential_resolver.py:66`):
  - `API_KEY` connections → Fernet-decrypt, return `{"type": "api_key", "value": ...}`
    (`:79-84`). **Static; no expiry; this entire report does not apply to API-key connections.**
  - OAuth → look up allauth `SocialToken`; fail closed on OCS team mismatch (`:87-95`); then
    `_aresolve_oauth_credential` (`:98-112`): refresh **only if**
    `token_needs_refresh(expires_at)` — i.e. expiry within `REFRESH_BUFFER = 5 min`
    (`token_refresh.py:19,43-51`). `expires_at is None` → "assume valid", never refresh
    (`token_refresh.py:49-50`).
  - On `TokenRefreshError`: **log a warning and proceed with the existing (near-expired or
    expired) token** (`credential_resolver.py:107-111`).

### 1.2 Transport (frozen by value)

- `tasks.py:277-283` → `_run_pipeline_with_progress(tm, credential, ...)` under
  `asyncio.to_thread` → `run_pipeline(tenant_membership, credential, pipeline_config, ...)`
  (`tasks.py:496`).
- `run_pipeline` (`mcp_server/services/materializer.py:96`) threads the same dict through
  DISCOVER (`:203`), every LOAD source (`:295-299`), and (for CommCare metadata) staging-asset
  generation. Loader constructors copy `credential["value"]` into session headers:
  - Connect: `Authorization: Bearer ...` (`mcp_server/loaders/connect_base.py:127`)
  - CommCare: `Bearer`/`ApiKey` (`commcare_base.py:26-34,47`)
  - OCS: `Bearer`/`X-api-key` (`ocs_base.py:40-43`)
- Even if the `SocialToken` row were refreshed in the platform DB mid-run, the run would never
  see it: the value is a string snapshot inside a `requests.Session` header.

### 1.3 Run duration is unbounded

`run_pipeline` has no overall deadline. CommCare cases/forms page at ≤1000 records/request
(`commcare_cases.py:13`, `commcare_forms.py:34-36`) with a 120 s per-request read timeout
(`commcare_base.py:19`); OCS does per-session detail fetches at upstream-default page size
(known finding); Connect adds dbt transform time. First syncs of large domains are
multi-hour by construction.

---

## 2. Exact failure surface on mid-run expiry

Traced chain (CommCare example; Connect/OCS identical in shape):

1. Token passes its TTL mid-load. Provider returns 401.
2. `CommCareBaseLoader._get` raises `CommCareAuthError("CommCare auth failed for domain X:
   HTTP 401")` (`commcare_base.py:62-67`). (Connect: `ConnectAuthError`, `connect_base.py:135,198`
   — note 401/403 is **not** in the retry forcelist, so auth failures are not retried;
   OCS: `OCSAuthError`, `ocs_base.py:47,69`.)
3. No handler anywhere maps `*AuthError` → refresh-and-retry. Inside the source loop the generic
   `except Exception` at `materializer.py:328` records the source as
   `{"state": "failed", "error": "CommCareAuthError: ... HTTP 401"}` (via `_summarize_error`,
   `:663-673`), marks all remaining sources `"skipped"` (`:345-350`), stamps the run terminal —
   **`PARTIAL`** if any source committed or any resumable cursor advanced, else **`FAILED`**
   (`:355-371`) — and re-raises. A DISCOVER-phase 401 lands in the pre-loop handler
   (`:404-428`) → run `FAILED` with `result.error`.
4. `materialize_workspace` catches it (`tasks.py:307-309`; `ConnectAuthError` is *not*
   `ConnectExportError`, so Connect 401s take the generic branch too) → tenant marked
   `{"success": False, "error": "..."}`; loop continues to the next tenant (whose credential is
   freshly resolved — see §4).
5. `finally:` defers `resume_thread_after_materialization` (`tasks.py:356-360`).
6. Resume task (`tasks.py:1020`): `_aggregate_materialization_state` (`:928`) yields
   `status="failed"` (all-failed) or `"partial"`.
   - `"partial"` → honest prompt (`:1109-1119`).
   - `"failed"` → falls into the **generic else branch** (`:1120-1125`): *"Materialization just
     completed (status=failed). Please continue with the user's original request **using the
     now-loaded data**."* — for a total failure there is no loaded data (see finding G2-CRED-4).
7. `ThreadJob` → `FAILED` with `error_summary` from `_compose_failure_summary`
   (`:1242-1257`, `:64-122`), e.g. *"forms failed: CommCareAuthError: CommCare auth failed for
   domain X: HTTP 401. cases (210,000 rows) loaded successfully."* The frontend failure card
   renders this from the jobs poll (subject to the known live-session `toolCallId`-mismatch
   finding, which can suppress the card during the live session).
8. **Nothing in the chain tells the agent or the user that the remedy is to re-authenticate.**
   The only credential-flavored guidance in the resume prompts is for the *missing*-credential
   case ("no credentials set up", `tasks.py:1103-1108`). An expired-token 401 is presented
   identically to an upstream permission revocation.

`TenantSchema`/catalog side effects: a first-run failure leaves the run terminal (the `:404-428`
handler exists precisely to avoid stuck-DISCOVERING rows), earlier sources stay committed
(PARTIAL), and the in-flight source's transaction rolls back inside `_load_and_commit_source`.

---

## 3. token_refresh.py reachability — complete call map

`refresh_oauth_token` has exactly **two** call sites (`grep` over `apps/`, `mcp_server/`,
`config/`):

| Caller | Process | When |
|---|---|---|
| `credential_resolver._aresolve_oauth_credential` (`credential_resolver.py:106`) | **worker** (only callers of `aresolve_credential` are `tasks.py:160` and `:264`) | once per tenant, at task start, only if <5 min to expiry |
| `auth_views.providers_view` (`auth_views.py:235-243`) | Django API (interactive) | `GET /api/auth/providers/` — settings/onboarding page poll |

- **MCP server: zero reachability.** `grep credential_resolver|token_refresh|SocialToken
  mcp_server/` → no matches. The MCP process never refreshes anything; its only token contact is
  the `oauth_tokens` meta extraction (`mcp_server/auth.py:13-25`), which is the already-known
  dead end-to-end plumbing.
- **Chat path: zero refresh.** `get_user_oauth_tokens` (`apps/agents/mcp_client.py:79-90`) reads
  `SocialToken.token` raw — no expiry check, no refresh — for both interactive chat
  (`chat/views.py:162`) and resume (`tasks.py:858`). Given the 15-minute CommCare TTL, the token
  this ships is *usually already expired*; harmless today only because the MCP-side consumer is
  dead.
- So the answer to the mandate question is: token_refresh **is** reachable from the worker, but
  **only at task start**; never mid-run; never from the MCP server; and the chat/agent path never
  refreshes at all.

The module docstring (`token_refresh.py:3-5`) claims it is "Called proactively (before token
expires) **and reactively (after 401)**". The reactive half is false — no 401 handler anywhere
invokes a refresh (finding G2-CRED-2).

---

## 4. Resume paths: stale token or re-resolve?

Both resume mechanisms **re-resolve** — this part of the architecture is sound:

- **Cursor-based resume (#187):** the cursor lives in `MaterializationRun.result.sources[*]
  .cursor_state` and is read by the *next* run (`materializer.py:252`,
  `_load_prior_resume_cursors`). The next run is always a fresh `materialize_workspace` dispatch
  (MCP tool `mcp_server/server.py:607` or the retry endpoint
  `api/materialization_views.py:185`), which re-runs `aresolve_credential` at `tasks.py:264` —
  fresh token, fresh proactive-refresh opportunity. **No stale token is replayed.**
- **`resume_thread_after_materialization`:** task args carry only `thread_job_id`
  (`tasks.py:393`); `oauth_tokens` are re-read at resume time (`tasks.py:858`) — not snapshot
  from the original chat config. (They're raw, unrefreshed reads into the dead MCP plumbing, but
  not *stale-from-config*.)
- **Multi-tenant loop:** tenant N's credential is resolved at tenant N's turn (`tasks.py:264` is
  inside the loop), so hours spent on tenants 1..N-1 don't pre-age tenant N's token.

The stale-credential window is therefore exactly **one tenant's `run_pipeline` invocation** —
which is also the only place where multi-hour durations occur.

---

## 5. The CommCare 429 / Retry-After interaction

The mandate hypothesized "retries extending run duration past token expiry". The reality splits
by provider:

- **CommCare:** the loaders have **no retry at all** (known finding — `commcare_base.py:61-69`
  is a bare GET + `raise_for_status`). A 429 fails the source on first sight. So retries do not
  extend CommCare runs; instead, HQ's *by-design* rate limiting makes the effective page rate
  lower, which lengthens the run and makes the 15-minute token ceiling bind sooner. The two
  known failure modes (429-fails-run, token-expiry-fails-run) **race**; whichever fires first,
  a long CommCare OAuth sync cannot finish.
- **Connect:** the urllib3 `Retry` policy (`connect_base.py:61-69`) sets
  `respect_retry_after_header=True` with 429 in the forcelist. Verified against the pinned
  urllib3 2.6.3 (`uv.lock`): `Retry.sleep()` sleeps the **full server-supplied Retry-After,
  uncapped** — `backoff_max` applies only to the computed exponential path. The comment at
  `connect_base.py:31-32` ("~14s worst case") is false whenever Retry-After is present: a server
  sending `Retry-After: 3600` produces up to 3 × 1 h synchronous sleeps inside the
  `asyncio.to_thread` worker thread. During those sleeps the cancellation checkpoint
  (between-pages `progress_updater`) cannot fire, and on the single-worker deployment (known
  finding) the platform's one background slot is occupied. For Connect's 2-week TTL this rarely
  crosses token expiry, but it extends runs and blocks Stop.
- **OCS:** no retry; 429 → `raise_for_status` → source fails (sibling of the known
  Connect-only-hardening finding).

---

## 6. Probability assessment: multi-hour first materialization on an OAuth-backed connection

| Provider (OAuth) | Token budget at load start | Run > budget? | P(mid-run auth failure) |
|---|---|---|---|
| CommCare HQ | ≤15 min (≤5 min if not refreshed at start; refresh only fires under 5 min remaining) | any domain beyond ~100–300k records at 1000/page + HQ latency/rate-limits ⇒ yes | **≈1.0 — and permanent: non-resumable, so every retry restarts from zero with another ≤15-min token. Large CommCare OAuth syncs can never complete.** |
| OCS | remainder of a 10 h token (age = time since login/refresh) | large teams: hours of per-session fetches | **moderate** — roughly run_duration / 10 h for a token of random age; rises across retries because OCS sources are also non-resumable |
| Connect | remainder of a 2-week token | almost never | **low tail (~run_duration/336 h per run)**; additionally softened by cursor resume on retry |

Aggravating interactions already on the known-findings list: the broken "Use an API Key"
onboarding form (404s) funnels new users toward OAuth, the very credential class that cannot
survive long loads; and CommCare loaders' missing 429 retry independently caps CommCare run
length. Note the March deployment already produced `AUTH_TOKEN_EXPIRED` errors from the
start-of-run variant of this bug (`d4dee2e`); the mid-run variant has simply not been load-tested
because (per the test-architecture findings) no CI path runs a real long materialization.

---

## Findings

### G2-CRED-1 — One credential snapshot per materialization run; CommCare's 15-minute OAuth TTL makes long CommCare OAuth syncs structurally impossible
**Status:** BROKEN-NOW · **Impact:** correctness · **Confidence:** verified-by-trace (mechanism and Scout-side chain fully quoted; the 15-min TTL is upstream `master` settings — production override possible but the in-code HIPAA comment makes that unlikely) · **Complexity:** accidental

Chain: `materialize_workspace` resolves once per tenant (`apps/workspaces/tasks.py:264`) →
refresh only if <5 min to expiry (`apps/users/services/credential_resolver.py:102`,
`token_refresh.py:19`) → static dict into `run_pipeline`
(`tasks.py:277,496` → `mcp_server/services/materializer.py:96-127`) → token baked into session
headers (`mcp_server/loaders/commcare_base.py:47`, `connect_base.py:127`, `ocs_base.py:40-43`) →
TTL passes mid-load (HQ: 15 min, `dimagi/commcare-hq settings.py:885-889`) → 401 →
`CommCareAuthError` (`commcare_base.py:62-67`) → source `failed`, rest `skipped`, run
`FAILED`/`PARTIAL` (`materializer.py:328-371`) → tenant `success:false` (`tasks.py:307-309`) →
ThreadJob `FAILED` (`tasks.py:1236-1257`). Non-resumable providers (CommCare, OCS) restart from
zero on retry with another ≤15-min/≤10-h token, so for CommCare the failure repeats on every
attempt. Reachable via: every OAuth-backed materialization (agent `run_materialization` tool,
retry endpoint, legacy refresh). OCS (10 h default TTL) and Connect (2 weeks) inherit the same
mechanism as latent/tail risk.

### G2-CRED-2 — No reactive 401 refresh exists anywhere; token_refresh.py docstring claims it does; AUTH_TOKEN_EXPIRED error code is producer-less residue
**Status:** DEBT · **Impact:** correctness · **Confidence:** verified-by-trace · **Complexity:** accidental

`token_refresh.py:3-5` says "Called proactively (before token expires) and reactively (after
401)". Grep over the repo: `refresh_oauth_token` has exactly two callers
(`credential_resolver.py:106` proactive-at-resolution; `auth_views.py:240` interactive
providers list). Every loader 401 raises `*AuthError` with no refresh-and-retry handler.
`AUTH_TOKEN_EXPIRED` (`mcp_server/envelope.py:31`, added with the #112 fix `7aca7ce`/`d4dee2e`)
has no producer — only a test asserting the constant exists (`tests/test_mcp_server.py:247-252`).
The fix for "tokens expired before use" was applied at resolution time only; the mid-run sibling
(G2-CRED-1) was left in place — a fixed-where-it-bit case.

### G2-CRED-3 — Refresh failure falls back to a known-stale token, and the resulting 401 carries no re-authentication guidance anywhere in the chain
**Status:** BROKEN-NOW (degraded UX on a reachable path) · **Impact:** correctness · **Confidence:** verified-by-trace · **Complexity:** accidental

`credential_resolver.py:107-111`: on `TokenRefreshError` (provider down, refresh token revoked,
e.g. user re-authorized elsewhere) the resolver logs a warning and returns the existing token —
which it just determined is within 5 minutes of expiry or already expired. The run then provisions
a schema, runs discover, and dies at the first authenticated request with
"`... auth failed ...: HTTP 401`". Failing closed here (like the OCS team-mismatch guard 20 lines
up) would convert a guaranteed-doomed multi-minute run into an immediate, attributable error.
Compounding: no message in the failure surface — `_summarize_error` output, `error_summary`
(`tasks.py:1255`), or any resume prompt — maps 401 to "reconnect your account"; the only
credential guidance is for the *missing*-credential case (`tasks.py:1103-1108`,
`materialization no credential configured` at `tasks.py:270`). Expired token, revoked access, and
wrong-permission tokens are indistinguishable to both agent and user.

### G2-CRED-4 — Resume prompt's else-branch tells the agent to "continue using the now-loaded data" for fully FAILED (and CANCELLED) runs
**Status:** BROKEN-NOW · **Impact:** correctness · **Confidence:** verified-by-trace · **Complexity:** accidental

`tasks.py:1075-1125`: the prompt branches are `view_schema_failed` / `"no_runs"` / `"partial"` /
else. `status="failed"` (all sources failed — exactly what a token-expiry-before-first-commit or
auth-revocation produces) and `"cancelled"` fall into the else branch: *"Materialization just
completed (status=failed). Please continue with the user's original request using the now-loaded
data. Per-tenant: {summary}"*. For a totally failed run there is no loaded data; the instruction
contradicts the embedded status and invites the agent to query empty/absent schemas — the
documented precondition for the #190 panic-loop class. The `"partial"` branch shows the intended
honest pattern; `"failed"` simply never got one. Reachable via: any fully failed materialization
with a waiting thread.

### G2-CRED-5 — Connect Retry-After honored uncapped inside the worker thread; "~14s worst case" comment false; sleep is uncancellable
**Status:** LATENT · **Impact:** cost-perf · **Confidence:** verified-by-trace (urllib3 2.6.3 `Retry.sleep` source inspected) · **Complexity:** accidental

`connect_base.py:61-69` sets `respect_retry_after_header=True` with 429/5xx in the forcelist;
urllib3 sleeps the full server-supplied `Retry-After` (uncapped — `backoff_max` only bounds the
computed exponential path), up to 3 times per request, synchronously inside the
`asyncio.to_thread` pipeline thread. The comment at `connect_base.py:31-32` promising "~14s worst
case" is wrong whenever the header is present. During the sleep the between-pages cancellation
checkpoint (`tasks.py:485-494`) cannot run, and on the single-worker deployment the platform's
only background slot is held. (CommCare/OCS have the opposite problem — no retry at all — already
reported.)

### G2-CRED-6 — Concurrent interactive refresh can revoke the access token a long-running load is using (DOT rotation)
**Status:** LATENT · **Impact:** correctness · **Confidence:** hypothesis · **Complexity:** accidental

`providers_view` (`auth_views.py:235-243`) refreshes near-expiry tokens whenever the settings/
onboarding page polls `/api/auth/providers/`. django-oauth-toolkit's refresh-token grant revokes
the previous access token server-side (standard DOT behavior — not verified against each
provider's deployed DOT version). A user who opens Settings while a materialization is running on
a near-expiry token would invalidate the token frozen in the run's session headers, killing the
run minutes earlier than TTL. Narrow window (only fires inside the 5-minute buffer, where the run
was about to die anyway); listed for completeness because it also applies to *Connect's* 2-week
tokens in their final 5 minutes.

---

## What's fine (verified healthy)

- **Cursor-based resume re-resolves credentials.** The watermark is data-only
  (`MaterializationRun.result.sources[*].cursor_state`); every retry is a fresh task that calls
  `aresolve_credential` anew (`tasks.py:264`, `api/materialization_views.py:185`,
  `mcp_server/server.py:607`). No stale token is ever replayed from task config.
- **`resume_thread_after_materialization` carries no tokens in its args** — only
  `thread_job_id`; `oauth_tokens` are re-read at resume time (`tasks.py:858`).
- **Per-tenant resolution inside the multi-tenant loop** (`tasks.py:264` is in the loop): hours
  spent on earlier tenants do not pre-age later tenants' tokens.
- **401/403 excluded from the Connect retry forcelist** — auth failures fail fast instead of
  burning 4 attempts (`connect_base.py:37,135,198`).
- **Proactive refresh, when it fires, persists correctly**: new access token, rotated refresh
  token, and recomputed `expires_at` are saved (`token_refresh.py:84-90`).
- **API-key connections are immune** to this whole class (static Fernet-encrypted strings,
  `credential_resolver.py:79-84`) — the OCS/CommCare API-key path has no TTL.
- **Auth failures cannot strand a run in DISCOVERING**: the pre-loop handler stamps a terminal
  FAILED state (`materializer.py:404-428`); the per-source handler stamps PARTIAL/FAILED with
  per-source errors that do reach `error_summary` and the resume prompt's per-tenant summary.

## Coverage log

**Deep-read (line-by-line):**
`apps/users/services/credential_resolver.py`, `apps/users/services/token_refresh.py`,
`apps/users/models.py`, `mcp_server/loaders/connect_base.py`,
`mcp_server/loaders/commcare_base.py`, `mcp_server/loaders/ocs_base.py`,
`mcp_server/services/materializer.py:90-520` and `:663-680` (run_pipeline, failure handling,
_summarize_error), `apps/workspaces/tasks.py:1-510` and `:840-1290` (materialize/refresh tasks,
resume task, aggregation, failure summaries), `apps/agents/mcp_client.py`,
`apps/users/auth_views.py:195-270` (providers_view).

**Skimmed:** `apps/workspaces/services/schema_manager.py` (provision only),
`apps/workspaces/api/materialization_views.py` (retry dispatch only), `mcp_server/server.py`
(run_materialization dispatch lines only), `mcp_server/loaders/commcare_cases.py` /
`commcare_forms.py` (page-size constants only), `mcp_server/auth.py`, `mcp_server/envelope.py`
(AUTH_TOKEN_EXPIRED), upstream `commcare-connect/config/settings/base.py` and
`open-chat-studio/config/settings.py` (OAUTH2_PROVIDER blocks), `dimagi/commcare-hq`
`settings.py` (fetched, OAUTH2_PROVIDER block only), urllib3 2.6.3 `Retry.sleep` source,
git history around `d4dee2e`/`7aca7ce`/`564906b`.

**Not examined (honest gaps):**
- `apps/users/services/tenant_resolution.py` and `signals.py` — login-time provider calls have
  their own AuthError classes and presumably their own token-staleness behavior (the known
  me_view/login-swallow findings touch this; I did not trace token freshness there).
- `apps/users/adapters.py` (`decrypt_credential`, allauth token persistence — I inferred, did not
  verify, that allauth populates `SocialToken.expires_at` from `expires_in` for the custom
  CommCare/Connect/OCS providers; if any provider leaves it NULL, `token_needs_refresh` returns
  False forever and the proactive refresh never fires for that provider).
- Whether each provider's *deployed* DOT config rotates/revokes access tokens on refresh
  (G2-CRED-6 is a hypothesis), and whether HQ production overrides the 15-minute TTL.
- OCS/Connect refresh-token TTLs (refresh-token expiry would eventually break even task-start
  refresh; not traced).
- `mcp_server/loaders/*` concrete loaders beyond cases/forms page-size constants; dbt/transform
  phase credential needs (dbt talks to the managed DB, not providers — assumed, not traced).
- Frontend rendering of `error_summary` (relied on known findings for the toolCallId-mismatch
  caveat); `tests/test_mcp_token_refresh.py` and `tests/test_oauth_tokens.py` beyond grep hits.
- The interactive chat 401 surface for MCP `query` (managed-DB path — no provider tokens — but I
  did not re-verify that no MCP tool makes provider API calls at query time).
