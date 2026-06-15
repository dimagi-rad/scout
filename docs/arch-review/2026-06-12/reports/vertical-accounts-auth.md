# Vertical Review: Accounts, Auth, OAuth, Tenant Resolution, Credentials, Merge

Reviewer mandate: own the account model end-to-end — users, sessions, OAuth
providers, tenant resolution, credential storage/refresh, account
merge/reconciliation, email-verification policy. Is the account model coherent?

Scope of files examined is in the Coverage Log at the end. All findings are
**report-only**; no code was changed. Confidence is labelled per finding per the
methodology's shared evidence standards.

---

## Verdict on coherence

The account model is **largely coherent and unusually well-defended for its
size** — the merge service, the fail-closed OCS team guard, the per-provider
email-domain allow-list, and the connection/membership archival lifecycle are
all real, tested, and internally consistent. The incoherence is concentrated at
**three seams**:

1. **Password-signup vs. allauth identity** — local signup bypasses allauth
   entirely and never creates an `EmailAddress`, so the email-based account
   linking that the rest of the system assumes cannot fire for these accounts.
2. **A fully dead `oauth_tokens` transport** that *looks* load-bearing (and
   *looks* broken for OCS) but is inert; real credentials are resolved
   server-side in the worker.
3. **Config/doc drift on who is allowed to sign in** — the documented default
   email-domain restriction does not match the code default, leaving OCS (and
   Connect) OAuth sign-in open by default.

### Capability-by-capability "actually functional" estimate

| Capability | Demo path | Integration edges |
|---|---|---|
| Email/password login + signup | ~100% | Signup bypasses allauth → no `EmailAddress` (F1); no rate-limit recording on signup |
| OAuth login (CommCare / Connect / OCS) | ~95% | OCS exact-`provider`-match only (latent), domain allow-list default drift (F3) |
| Tenant resolution (3 providers) | ~90% | `me_view` re-resolves uncached for token-but-no-tenant users; transient `onboarding_complete=True` with zero tenants (F5) |
| Credential resolution (API-key + OAuth, team guard) | ~95% | Fail-closed multi-team OCS cap surfaces as generic "No credential" (F4) |
| OAuth token refresh | ~100% | only proactive/near-expiry; reactive-after-401 refresh not wired in resolver |
| Account merge (signal + operator command) | ~95% | gated on verified `EmailAddress` that F1 prevents for signup users |
| `oauth_tokens` → MCP transport | 0% (dead) | entire chain inert; OCS omission is a red herring (F2) |

---

## Findings

### F1 — Local signup bypasses allauth and never creates an `EmailAddress`; OAuth-with-same-email can never link to a password account
- **Status:** LATENT · **Impact:** correctness · **Complexity:** accidental ·
  **Confidence:** verified-by-trace
- **Reachable via:** `POST /api/auth/signup/` then later an OAuth login (any
  provider) that returns the same email.

`signup_view` creates the user with the raw manager and logs them in, never
touching allauth's account pipeline:

- `apps/users/auth_views.py:165` — `user = UserModel.objects.create_user(email=email, password=password)`
- `apps/users/auth_views.py:172` — `login(request, user, backend="django.contrib.auth.backends.ModelBackend")`

No `EmailAddress` row is created anywhere on user creation — `grep EmailAddress
apps/` shows it is only read in `signals.py`/`merge.py`, never written outside
merge. The whole identity-linking design depends on `EmailAddress`:

- allauth's `SOCIALACCOUNT_EMAIL_AUTHENTICATION = True`
  (`config/settings/base.py:212`) matches an incoming OAuth login to an existing
  user **by `EmailAddress`**. A password-signup user has none → no match.
- The custom reconcile gate requires a **verified** `EmailAddress` on the
  canonical user before auto-merging:
  `apps/users/signals.py:104` —
  `EmailAddress.objects.filter(user=canonical, email__iexact=new_email, verified=True).exists()`;
  on miss it logs "Refusing auto-merge" and returns (`signals.py:109-115`).

Consequence chain: a user signs up with email+password (no `EmailAddress`), then
logs in via CommCare/OCS with the same email. allauth can't match them by email,
and the reconcile signal refuses to merge because the canonical (password) user
has no verified `EmailAddress`. Result is **two separate accounts** with
fragmented tenants/workspaces, only reconcilable by the operator command
(`manage.py merge_duplicate_users`, which does *not* enforce the verification
gate — `merge_duplicate_users.py:72-79` groups purely by lowercased email).

The refusal itself is **secure-by-design** (the project's own test
`tests/test_social_login_reconciliation.py:156` documents this as anti-hijack),
so this is not a security hole. It is an **incoherence**: the system advertises
email-based cross-provider linking (`test_auth_settings.py` pins those settings
on) but silently can't deliver it for any account born through its own
`/signup/` endpoint. Worst-case edge (strong-inference, not traced through
allauth internals): if allauth instead attempts to *create* a fresh OAuth user
with that email, `User.email` is `unique=True` (`models.py:59`) and the insert
would `IntegrityError`, failing the OAuth login outright. This edge deserves a
dedicated integration test.

---

### F2 — The `oauth_tokens` transport (chat → graph → MCP `_meta` → `TenantContext`) is dead end-to-end; OCS omission is a red herring
- **Status:** DEBT · **Impact:** velocity (with a latent correctness trap) ·
  **Complexity:** accidental · **Confidence:** verified-by-trace
- **Reachable via:** every `POST /api/chat/` request builds and threads this data.

The chat view builds an `oauth_tokens` dict and threads it through three sinks,
none of which consume it:

1. `apps/chat/views.py:162` — `oauth_tokens = await get_user_oauth_tokens(user)`.
   That function (`apps/agents/mcp_client.py:79-89`) filters to
   `COMMCARE_PROVIDERS = {"commcare", "commcare_connect"}` — **OCS is excluded**.
2. Passed to `build_agent_graph(..., oauth_tokens=oauth_tokens)`
   (`chat/views.py:172,184`). In the graph, `oauth_tokens` appears **only** in
   the signature and docstring (`apps/agents/graph/base.py:485,495`) — it is
   never read. The injecting tool node injects only
   `workspace_id/user_id/thread_id/tool_call_id` (`graph/base.py:504-512,534`),
   not tokens.
3. Placed at `config["oauth_tokens"]` (`chat/views.py:196`,
   and again in the resume task `apps/workspaces/tasks.py:1154`). This is a
   **top-level** config key, not under `configurable`, so LangGraph neither
   injects it into tool `_meta` nor persists it to the checkpointer.

On the MCP side, `extract_oauth_tokens(meta)` (`mcp_server/auth.py:13`) is
**never called** in production — `grep` finds it only in `tests/test_mcp_server.py`.
`TenantContext.oauth_tokens` (`mcp_server/context.py:42`) is declared but never
populated or read. `mcp_server/envelope.py:82` scrubs an `oauth_tokens` key that
is never present.

The actual credential path is independent and healthy: `run_materialization`
defers `materialize_workspace` with only `workspace_id`/`user_id`
(`mcp_server/server.py:607-610`), and the worker re-resolves per-tenant via
`aresolve_credential(tm)` (`apps/workspaces/tasks.py:264`), which *does* handle
OCS and applies the team guard.

Why this matters beyond dead code: the OCS exclusion in `get_user_oauth_tokens`
is a **trap**. It reads as a bug ("OCS tokens don't reach the agent"), inviting a
future fix that adds OCS to `COMMCARE_PROVIDERS` — which would do nothing,
because the entire transport is inert. This is exactly the prompt/contract-drift
class the cartography flagged on this spine. Recommend deleting the transport
(both ends) or wiring it, not leaving it half-built.

---

### F3 — `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` default contradicts its documentation: OCS/Connect OAuth sign-in is unrestricted by default, not dimagi.com-only
- **Status:** LATENT · **Impact:** security · **Complexity:** accidental ·
  **Confidence:** verified-by-trace
- **Reachable via:** any OCS OAuth sign-in on a deployment that didn't set the env var.

The code default restricts **only** commcare:

- `config/settings/base.py:247-253` — `default={"commcare": ["dimagi.com"]}`.

The `.env.example` documents a different, broader default:

- `.env.example:44` — "Defaults to `["dimagi.com"]` for each of commcare,
  commcare_connect, and ocs if unset."

The enforcement point is `EncryptingSocialAccountAdapter.pre_social_login`
(`apps/users/adapters.py:84-104`): a provider **absent** from the dict is
unrestricted (`allowed = ... or []; if not allowed: return`). With the real
default, **OCS** (which returns an email via the `openid` scope —
`providers/ocs/provider.py:extract_common_fields`) accepts sign-ins from **any
email domain**, despite the documentation claiming a dimagi.com restriction.
Combined with `SOCIALACCOUNT_AUTO_SIGNUP = True` (`base.py:205`), this means any
OCS-authenticated user can self-provision a Scout account, get tenants
auto-resolved, and auto-create workspaces — unless an operator notices the doc
is wrong and sets the env var. (Connect is moot regardless: it returns no email,
and a no-email login bypasses the check by design — `adapters.py:88-90`.)

This is either a missing default or a stale doc; either way the two disagree on
a security control. Pick one and make them match, and add a settings test
mirroring `tests/test_auth_settings.py` that asserts the intended default.

---

### F4 — Multi-team OCS users can materialize only the team their current OAuth token is scoped to; the fail-closed result surfaces as a generic "No credential configured"
- **Status:** DEBT · **Impact:** correctness (UX/observability) ·
  **Complexity:** essential (single OAuth connection per provider is a real
  constraint) · **Confidence:** verified-by-trace
- **Reachable via:** an OCS user in ≥2 teams who materializes a chatbot from a
  team other than the one their live token is scoped to.

A user has at most one OCS OAuth `TenantConnection`
(`models.py:181-189` unique partial constraint). Each membership records the
team it was resolved under (`tenant_resolution.py:163` stamps
`tm.team_slug = team_slug` from the OIDC `team` claim). At credential time the
guard fails closed when the live token's team ≠ the membership's team:

- `apps/users/services/credential_resolver.py:92` — `if not token_obj or _oauth_team_mismatch(membership, token_obj): return None`
- `_oauth_team_mismatch` (`credential_resolver.py:52-63`) compares
  `membership.team_slug` to `token_obj.account.extra_data["team"]`.

The guard is correct and safe (no cross-team data leak). The cost: a user who
re-authorized into team B can no longer materialize team-A chatbots until they
re-auth into team A, and the failure is reported to the agent as the generic
`"No credential configured"` (`apps/workspaces/tasks.py:266-272`) — the same
message used for "you have no connection at all." A user with a *valid* OCS login
gets an indistinguishable error from a user with none. This is essential
complexity in the model but **accidental** in its observability: the agent/user
cannot tell "wrong team, re-auth" from "no credential." Recommend a distinct
error for the team-mismatch branch.

---

### F5 — `me_view` lazy onboarding resolution is uncached and returns transient `onboarding_complete=True` for token-but-no-tenant users, re-hitting provider APIs on every poll
- **Status:** LATENT · **Impact:** cost-perf (and a correctness wrinkle) ·
  **Complexity:** accidental · **Confidence:** verified-by-trace
- **Reachable via:** `GET /api/auth/me/` for a user with a valid OAuth token that
  grants access to zero tenants (e.g. a CommCare account with no domains).

`me_view` recomputes onboarding from the DB and, when incomplete, eagerly
resolves all three providers with **no cache guard**:

- `apps/users/auth_views.py:72-90` — DB check
  (`TenantMembership ... connection__isnull=False, archived_at__isnull=True`),
  then `_atry_resolve_provider` for commcare, connect, ocs.
- `_atry_resolve_provider` (`auth_views.py:46-56`) returns `True` whenever a
  token exists **and** `resolve_fn` does not raise — **even if it resolved zero
  tenants** (e.g. `resolve_commcare_domains` loops over an empty list and returns
  `[]` without raising, `tenant_resolution.py:43-68`).

Two consequences:

1. **Cost / provider hammering.** Contrast `tenant_list_view`, which wraps each
   resolution in a 1-hour cache (`apps/users/views.py:91-121`,
   `TENANT_REFRESH_TTL = 3600`). `me_view` has no such guard, so for a
   token-bearing user whose persistent membership count stays zero, **every**
   `/me` call issues up to three live provider API calls. The SPA polls `/me`
   (`frontend/src/store/authSlice.ts:36`).
2. **Transient inconsistency.** Because `_atry_resolve_provider` returns `True`
   on a zero-tenant success, `me_view` reports `onboarding_complete=True`
   (`auth_views.py:90`) for that one response while the persisted state (no
   membership) remains "incomplete." The next call recomputes `False` from the
   DB and re-resolves — the flag flaps and never stabilizes.

Fix direction: gate the lazy resolution behind the same per-provider cache as
`tenant_list_view`, and derive `onboarding_complete` from persisted memberships
only (not from the ephemeral resolve result).

---

### F6 — `disconnect_provider_view` deletes `TenantConnection` rows by the URL `provider_id`, which is the allauth provider *class* id, while `SocialAccount.provider` may be a custom `provider_id`
- **Status:** LATENT · **Impact:** correctness · **Complexity:** accidental ·
  **Confidence:** strong-inference (not reproduced; depends on operator config)
- **Reachable via:** `POST /api/auth/providers/<provider_id>/disconnect/` on a
  deployment where a `SocialApp.provider_id` was customized away from the class id.

The view handles the token side of the class-id/provider-id split defensively
(`apps/users/auth_views.py:182-194` falls back to
`SocialApp.objects.filter(provider=provider_id).values_list("provider_id")`),
but the connection/membership archival side uses the raw URL value:

- `apps/users/auth_views.py:198-206` — `TenantConnection.objects.filter(user=..., provider=provider_id, credential_type=OAUTH)` then archives memberships and deletes the connection.

`TenantConnection.provider` is always written as the **class** id
(`tenant_resolution.py:47,93,133` create with `provider="commcare"` etc.). In the
standard `setup_oauth_apps` deployment, `SocialApp.provider` == class id, so the
URL value matches and this is fine. But the `providers_view` comments
(`auth_views.py:258-262`) explicitly anticipate `SocialAccount.provider` being a
custom `provider_id` like `"commcare_prod"`. If that ever happens, the token
deletion succeeds (it has the fallback) but the `TenantConnection`/membership
archival silently no-ops — tokens revoked, but chatbots stay "connected" and
their auto-workspaces remain. Low likelihood in current deployments; flagged so a
future multi-instance provider config doesn't reintroduce it. Recommend resolving
the connection by the same class-id mapping the token branch already computes.

---

### F7 — `auto_create_workspace_on_membership` performs three writes outside a transaction
- **Status:** DEBT · **Impact:** correctness · **Complexity:** accidental ·
  **Confidence:** strong-inference
- **Reachable via:** every first-time TenantMembership creation (OAuth resolution
  or API-key connection).

`apps/users/signals.py:42-52` creates `Workspace`, then `WorkspaceTenant`, then
`WorkspaceMembership` with no `transaction.atomic()`. A failure between steps
(e.g. DB hiccup after the `Workspace.create`) leaves an orphan auto-workspace
with no tenant and/or no membership — which `load_workspace_context` would later
reject with "Workspace has no tenants" (`mcp_server/context.py:106-107`). The
idempotency guard (`signals.py:34-40`) is sound for the normal case (membership
creation is unique-constrained on `(user, tenant)` so the signal fires once per
pair), so this is a durability gap, not a duplication race. Wrap the three writes
in `transaction.atomic()`.

---

## What's actually fine (verified healthy)

- **Merge service correctness** (`apps/users/services/merge.py`). Field-level
  rules, EmailAddress dedupe + primary normalization, TenantMembership/Connection/
  WorkspaceMembership conflict resolution (role-rank upgrade), `_meta`-driven
  long-tail FK repoint with an explicit special-case skip set, all inside one
  `transaction.atomic()` with delete-last. The dry-run path mirrors the real path
  counts. The OAuth-connection conflict-merge respects the one-OAuth-per-provider
  constraint. Well-structured and covered by `test_merge_users_service.py`.
- **Fail-closed OCS team guard** (`credential_resolver.py:52-95`). Correctly
  returns `None` on token-team/membership-team mismatch; API-key path returns
  early before the guard (appropriately, since API keys aren't team-scoped tokens).
- **Token encryption at rest** (`apps/users/adapters.py`). SocialToken
  token/token_secret Fernet-encrypted via serialize/deserialize hooks; same key
  path as `encrypt_credential`/`decrypt_credential`; decrypt failure logs and
  returns "" rather than crashing.
- **Async credential resolution discipline.** Async tasks correctly use
  `aresolve_credential` (`tasks.py:160,264`) — the SynchronousOnlyOperation class
  the cartography flagged is avoided on the live materialize/refresh paths.
- **Auth domain restriction enforcement** (`adapters.py:pre_social_login`). The
  allow-list logic itself is correct and tested
  (`test_oauth_domain_restriction.py`); only the *default value* drifts from docs
  (F3).
- **Rate limiting** (`apps/users/rate_limiting.py`). `get_or_set` + `incr` with a
  TTL and a race-safe fallback on the expiry window; wired into `login_view`.
- **API-key provider strategy registry** (`services/api_key_providers/`). Clean
  strategy pattern; verify-and-discover on add, verify-for-tenant on rotate;
  CommCare/OCS implemented, registry-driven form schema exposed to the frontend.
- **Credential/connection archival lifecycle.** Disconnect and connection-delete
  both archive memberships (retain data, null the connection) rather than
  hard-deleting, enabling restore on reconnect; `onboarding_complete` derives from
  live (non-archived, connection-bearing) memberships.

---

## Coverage Log (honest — drives the gap loop)

### Deep-read (line-by-line)
- `apps/users/models.py`
- `apps/users/auth_views.py`
- `apps/users/views.py`
- `apps/users/services/tenant_resolution.py`
- `apps/users/services/credential_resolver.py`
- `apps/users/services/merge.py`
- `apps/users/services/token_refresh.py`
- `apps/users/services/ocs_team.py`
- `apps/users/services/api_key_providers/{__init__,base,registry,commcare,ocs}.py`
- `apps/users/signals.py`
- `apps/users/adapters.py`
- `apps/users/decorators.py`
- `apps/users/rate_limiting.py`
- `apps/users/auth_urls.py`, `apps/users/apps.py`, `apps/users/admin.py`
- `apps/users/providers/{commcare,commcare_connect,ocs}/{provider,views}.py`
- `apps/users/management/commands/{setup_oauth_apps,merge_duplicate_users}.py`
- `apps/users/migrations/0007_migrate_credentials_to_connections.py` (forward fn)
- `apps/workspaces/workspace_resolver.py`
- `mcp_server/context.py`, `mcp_server/auth.py`
- `config/settings/base.py` (auth/allauth/session/CSRF block only)
- `apps/chat/views.py` (oauth_tokens + workspace-resolution portions, ~140-243)
- `apps/agents/mcp_client.py` (`get_user_oauth_tokens`)
- `apps/agents/graph/base.py` (oauth_tokens param + injection node, ~480-540)
- `apps/workspaces/tasks.py` (credential-resolution sites: refresh ~130-200,
  materialize ~204-289, resume oauth ~1140-1165)
- `mcp_server/server.py` (`run_materialization`, ~521-640)

### Skimmed (read for shape / one-direction)
- `tests/test_social_login_reconciliation.py`, `tests/test_auth_settings.py`
- `.env.example` (OAuth + allow-list section)
- `frontend/src/api/auth.ts`, `frontend/src/store/authSlice.ts` (shape only)
- `apps/users/migrations/0006_tenant_connections.py` (help_text grep)

### NOT examined (gaps for a later pass)
- The **end-to-end allauth OAuth callback HTTP flow** in a running browser —
  F1's worst-case (unique-email IntegrityError on fresh OAuth signup vs.
  graceful connect) is **inferred**, not reproduced. Needs an integration test
  exercising signup-then-OAuth-same-email.
- **allauth internals** (`_lookup_by_email`, `_lookup_by_socialaccount`,
  `EMAIL_AUTHENTICATION` matching) — treated as a black box; claims about what
  allauth does on no-EmailAddress match are strong-inference.
- The bulk of **`apps/users` tests** beyond the two skimmed:
  `test_merge_duplicate_users_command.py`, `test_merge_users_service.py`
  (internals), `test_tenant_*`, `test_oauth_tokens.py`, `test_users.py`,
  `test_api_key_providers_view.py`, `test_tenant_credentials_{post,patch}.py`,
  `test_me_view_connect_resolution.py` — not opened; "what do the mocks hide"
  is a test-architecture-lens question I did not pursue.
- **Connect/CommCare/OCS provider API payload shapes** (pagination, id/email
  fields) — trusted the resolver code's assumptions; did not cross-check against
  live provider contracts (Connect `opp_org_program_list`, OCS `/api/experiments/`,
  CommCare `user_domains`).
- **Session/cookie security posture** beyond names (`SESSION_COOKIE_NAME`,
  `CSRF_COOKIE_*`) — did not audit production.py cookie flags (Secure/SameSite/
  HSTS) against the dev defaults; deferred to the ops/security lens.
- **Google/GitHub providers** — present in `INSTALLED_APPS`/`setup_oauth_apps`
  but no tenant resolution; their interaction with the merge/reconcile path
  (they return verified emails) was not traced.
- **Frontend onboarding/connections UX** (`OnboardingWizard`, `ConnectionsPage`,
  `ApiConnectionDialog`) — only confirmed the `onboarding_complete` field is
  consumed; did not review the flows.
- **`config/settings/production.py`** auth-relevant overrides — not opened.
