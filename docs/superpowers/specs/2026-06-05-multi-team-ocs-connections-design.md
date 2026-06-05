# Multi-team OCS connections

## Problem

A Scout user who works with **more than one Open Chat Studio (OCS) team** cannot reliably materialize their chatbots. Concretely (the reported bug): a user logs into OCS via OAuth (one team), later adds an API key (another team), and materialization then fetches some chatbots with the *wrong* team's credential → OCS returns 404/403.

Two root causes:

1. **No model of a credential's team.** OCS scopes every credential to exactly one team (verified below). Scout stores a single `TenantCredential` **per chatbot**, with no record of which team a chatbot belongs to, and credential resolution can return any credential the user owns — including one scoped to a different team.

2. **No first-class "connection".** Today's model and UI treat each *chatbot* as the unit. One OCS API key = one team = N chatbots, but the key is stored (encrypted) duplicated across N per-chatbot rows, invisible and unmanageable as a unit. There is no way to add/label/rotate/remove "a key for team X", or to see which teams are connected. Re-pasting a key that overlaps an existing chatbot silently overwrites that chatbot's single credential.

## Ground truth (verified against `dimagi/open-chat-studio` source)

- **1 credential = 1 team.** `UserAPIKey.team` (FK, required) and `OAuth2AccessToken.team` (FK, set at authorize-time) each bind to exactly one team. `/api/experiments/` always filters `team = request.team`; it never spans teams. So every chatbot a credential can list belongs to that credential's team, and covering N teams requires N credentials.
- **No public `/api/teams/` endpoint.** The DRF router exposes only `experiments`, `sessions`, `participants`, `files`, `openai`, `trigger_bot`, `chat`. There is no whoami/me/current-team endpoint.
- **The experiment API `url` field does NOT contain the team slug** — it is `https://host/api/experiments/<uuid>/`. The team-slug web URL (`/a/<team_slug>/...`) is `Experiment.get_absolute_url`, which the API does not return. The experiment object carries no team field.
- **The OCS team is available at OAuth login.** OCS is a full OAuth2/OIDC server; `get_additional_claims` sets `claims["team"] = team.slug`, gated behind the `openid` scope — which Scout's OCS provider already requests (`OCSProvider.get_default_scope` → `[..., "openid"]`). The slug therefore arrives in the `/o/userinfo/` response and is stored in `SocialAccount.extra_data["team"]`.
- **Team name+slug appear in `/api/sessions/`** (nested `team` object), available whenever the team has ≥1 session. `GET /api/sessions/?page_size=1` → `results[0].team.{name,slug}` (cursor pagination, no `count`). This is the only API-key-reachable source of a team's name/slug; it returns nothing only when the team has zero sessions.

## Scope

In scope:
- A first-class **`TenantConnection`** model: one row per credential the user added (one OAuth login, or one API key) — credential-only.
- Per-chatbot team identity on **`TenantMembership`** (`team_slug`/`team_name`) plus a `connection` FK and an `archived_at` soft-delete marker.
- Credential resolution selects a chatbot's connection, **failing closed** when an OAuth token has moved to a different team than the chatbot's.
- Import flows for OAuth login and API-key add (OCS **and** CommCare) that create/maintain connections and stamp each chatbot's team.
- API-key team auto-detection (OCS) with a typed-name fallback.
- Connections-page rework: list connections, each with its chatbots grouped under it (labeled by team); add/rotate/remove operate per connection.
- A data migration that replaces `TenantCredential` with `TenantConnection`, preserving existing access.

Out of scope (v1):
- **N OAuth teams.** OAuth covers one team (the login team), because allauth stores one OCS token per identity. Additional teams use API keys (unlimited). The model already records each chatbot's `team_slug`, so N-team OAuth is a later additive change with no migration churn.
- Cross-connection fallback (if a chatbot's OAuth connection goes stale and a separate API-key connection for the same team exists, v1 fails closed rather than auto-switching).

## Data model

### New: `TenantConnection` (credential only)

One row = one credential the user added.

| field | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `user` | FK → User (CASCADE) | owner |
| `provider` | char, `PROVIDER_CHOICES` | `commcare` / `commcare_connect` / `ocs` |
| `credential_type` | char | `oauth` / `api_key` |
| `encrypted_credential` | char(2000), blank | Fernet-encrypted API key; `""` for OAuth (token lives in allauth `SocialToken`) |
| `created_at` / `updated_at` | datetime | |

Constraint: partial unique on `(user, provider)` **where `credential_type = "oauth"`** — **at most one OAuth connection per user per provider** (v1 has no multi-team OAuth). API-key connections have no uniqueness (a user may add several per provider).

The connection holds **no team fields** — a connection is a credential. Team identity lives on the chatbot (below), which is its single source of truth.

### `TenantMembership` changes

Add:
- `connection = FK → TenantConnection (null=True, on_delete=SET_NULL, related_name="memberships")` — which connection grants this user access to this chatbot/domain/opp.
- `team_slug = CharField(blank, default="")` — the chatbot's OCS team slug (`""` for CommCare/Connect, or when undetectable). Immutable per chatbot.
- `team_name = CharField(blank, default="")` — display label for the chatbot's team.
- `archived_at = DateTimeField(null=True, blank=True)` — when set, the membership and everything under its tenant (chatbot, conversations, materialized data) is **hidden** from all listings but retained on disk.

Behavior:
- `null=True` connection: a membership may have no connection (while archived); resolution treats null as "no usable credential."
- Removing a connection **archives** its memberships rather than deleting them; re-adding the same credential later un-archives and re-links them, restoring the chatbots and their conversations/data.

`TenantCredential` is deleted (model + table) by the migration; all code references move to `TenantConnection`.

## Credential resolution

`resolve_credential(membership)` / `aresolve_credential(membership)`:

1. `conn = membership.connection` (select_related). If `None` → return `None`.
2. If `conn.credential_type == API_KEY` → decrypt `conn.encrypted_credential` → `{"type": "api_key", "value": ...}`. (An API-key connection only ever imported chatbots in its own team, so its key is valid for them.)
3. Else (OAuth):
   a. Look up the allauth `SocialToken` for `(user, conn.provider)` (with `.account` for `extra_data`). None → return `None`.
   b. **Stale-token guard:** if `membership.team_slug` is non-empty, read `account.extra_data.get("team")` (the team the current OAuth token is scoped to). If it is present and `!= membership.team_slug` → return `None` (fail closed — the token has moved to another team; do not fetch this chatbot's data with it). If `team_slug` is empty (CommCare/Connect/legacy) or the current team is unknown, skip the guard.
   c. Return the OAuth token (refreshing near expiry).

This fixes the bug and its variant: re-logging-in OAuth as a different team makes the prior team's chatbots resolve to `None` (UI shows "needs reconnect") instead of silently using the wrong-team token and 404-ing. The guard compares the chatbot's own team (`membership.team_slug`) against the real OIDC `team` claim — it is used only to detect a moved token.

## Import flows

### OAuth login (`resolve_ocs_chatbots`, and CommCare/Connect equivalents)

- `get_or_create` the single OAuth connection for the provider: `TenantConnection(user, provider, credential_type=OAUTH)` (the partial-unique constraint guarantees one).
- OCS: read the team slug from `extra_data["team"]`; resolve a friendly `team_name` via `GET /api/sessions/?page_size=1` → `results[0].team.name` when a session exists, else the slug.
- For each chatbot/domain/opp the token lists: `get_or_create` the `Tenant` + `TenantMembership`; set `membership.team_slug`/`team_name` (OCS: detected values; CommCare/Connect: `""`), clear `archived_at` if set, set `membership.connection = <oauth connection>`.

### API-key add (`POST /api/auth/connections/`) — OCS and CommCare

- `verify_and_discover` (existing per-provider strategy) verifies the key and lists its tenants (OCS chatbots / CommCare domains).
- **OCS team detection** (verified-real endpoints only): call `GET /api/sessions/?page_size=1`. If a session exists → authoritative `team_slug` + `team_name` from `results[0].team`. If the team has **no sessions** (empty `results`) → `team_slug=""` and use the user-supplied team name. `OCSStrategy.form_fields` gains an **optional** `team_name` field (`editable_on_rotate=False`), used only as the fallback; if auto-detection finds nothing and the field is blank → 400 asking for a team name. CommCare keys have no team concept (`team_slug=""`, `team_name=""`); `CommCareStrategy.form_fields` is unchanged.
- Create a new `TenantConnection(user, provider, credential_type=API_KEY, encrypted_credential=encrypt(pack(fields)))`. Each add = a new connection; the same team added twice yields two connections, removable independently. Rotation uses Edit, below.
- For each tenant the key lists: `get_or_create` `Tenant` + `TenantMembership`; set `team_slug`/`team_name` (OCS detected/typed; CommCare `""`), clear `archived_at`, set `membership.connection = <this connection>` (repoint if it previously pointed elsewhere — the most recent import owns the chatbot).

### Rotate / remove

- **Rotate** (`PATCH /api/auth/connections/<id>/`): re-verify the new key against the connection's known tenants (`verify_for_tenant`), then update `encrypted_credential` on the single connection row. (No more per-chatbot rotation.)
- **Remove** (`DELETE /api/auth/connections/<id>/`): set `archived_at=now()` on the connection's memberships (hiding their chatbots while retaining conversations/materialized data), then delete the `TenantConnection` (`SET_NULL` clears `membership.connection`). Re-adding the credential later un-archives and re-links. Shared `Tenant`/`Workspace` rows are untouched.
- **Disconnect OAuth provider** (`POST /api/auth/providers/<id>/disconnect/`): in addition to removing the allauth account/token, archive its memberships and delete that provider's OAuth `TenantConnection` for the user.

## API / view changes

- New connection-centric endpoints under `auth_urls.py`, replacing the per-chatbot `tenant-credentials/` surface (the frontend is the only consumer and is updated in this PR):
  - `GET /api/auth/connections/` → connections, nested:
    ```json
    [{ "connection_id", "provider", "credential_type",
       "chatbots": [{ "membership_id", "tenant_id", "tenant_name", "team_slug", "team_name" }] }]
    ```
    A connection's team label is derived from its chatbots (for an API-key connection they share one team; for the OAuth connection, chatbots show their own team).
  - `POST /api/auth/connections/` → add an API-key connection (`{provider, fields}`).
  - `PATCH /api/auth/connections/<id>/` → rotate the key. `DELETE /api/auth/connections/<id>/` → remove the connection.
- `me_view` / `login_view`: `onboarding_complete` becomes "user has ≥1 `TenantConnection`".
- All membership listings exclude archived rows (`archived_at__isnull=True`): `GET /api/auth/connections/`, `tenant_list_view`, and `materialize_workspace`'s membership query.
- `tasks.py` materialization: `select_related(..., "connection")` and pass `membership` to the resolver (unchanged call signature).

## Frontend changes (`ConnectionsPage`, `ApiConnectionDialog`)

- **API Key Connections** section becomes **Connections**: render one card/group per connection, showing its team label (or provider for non-team), a `credential_type` badge, and the chatbots under it. Edit (rotate) and Remove act on the connection.
- `ApiConnectionDialog` (add): for OCS, show **API Key** + an **optional Team name** field labeled "Team name (auto-detected if left blank)". For CommCare, fields are unchanged (username + API key). Edit mode shows only `editable_on_rotate` fields, unchanged.
- `data-testid`s per QA conventions: `connection-card-<id>`, `connection-team-<id>`, `add-connection-button`, `api-connection-field-team_name`, `rotate-connection-<id>`, `remove-connection-<id>`.

## Data migration

A single migration: create `TenantConnection`, add `TenantMembership.connection`, `team_slug`, `team_name`, `archived_at`, copy data, drop `TenantCredential`. Lossless (preserves current access); grouping of legacy keys is best-effort.

Forward (per existing `TenantCredential` `cred`, OneToOne to a membership `tm`):
- **OAuth creds:** `get_or_create` one connection `(user=tm.user, provider=tm.tenant.provider, credential_type=oauth)` and set `tm.connection`. Collapses a user's OAuth memberships into one connection per provider (matching the new constraint).
- **API-key creds:** create one connection per `cred` (Fernet ciphertext can't be deduped to recover which chatbots shared a key), copying `encrypted_credential`; set `tm.connection`. Legacy keys are therefore ungrouped until re-added; re-adding a key produces a properly grouped, team-labeled connection.
- `team_slug`/`team_name` on migrated memberships are left `""` (legacy team unknown); resolution's guard is skipped for empty `team_slug`, preserving today's behavior until the user re-authenticates / re-adds the key.

Reverse migration recreates a `TenantCredential` per membership from its `connection` (best-effort, for dev reversibility).

## Testing (TDD)

`tests/test_ocs_connections.py` (new) + updates to existing tenant/credential tests:
- **Resolution:** API-key connection resolves; OAuth resolves when `extra_data["team"]` matches `membership.team_slug`; **fails closed (None)** on mismatch; returns None when `connection` is null; CommCare/Connect OAuth (empty `team_slug`) resolves without the guard.
- **The reported bug, end-to-end:** OAuth team A imports chatbots → add API key team B → re-login OAuth as team B; assert team-A chatbots resolve to None (not the team-B token) and team-B chatbots resolve correctly.
- **Import:** API-key add creates one connection and links all its chatbots with the detected team; a second key (different team) creates a separate connection and does not clobber the first; OAuth import links chatbots to the single OAuth connection without touching API-key connections; CommCare API-key add works (team fields empty).
- **Team auto-detect:** API-key add reads team from `/api/sessions/?page_size=1`; a team with no sessions falls back to the typed name (and 400s if none given).
- **Rotate/remove/archive:** rotate updates the one connection; remove archives its memberships (hidden from listings, data retained); re-adding the credential un-archives and re-links them.
- **Migration:** maps existing OAuth/API-key credentials onto connections preserving resolution.
- Existing OCS loader/materializer/tenant-resolution tests stay green.

## Rollout / limitations

- v1 OAuth = one team (the login team); API keys = unlimited teams (OCS and CommCare). The Connections UI states this where a user might expect to add a second OAuth team.
- Stale OAuth chatbots are shown but uncredentialed (honest "needs reconnect"), not hidden or silently wrong.
- Every OCS call is one verified to exist: `/api/experiments/`, `/api/sessions/`, `/o/userinfo/`, `/o/token/`. No endpoints are invented.
