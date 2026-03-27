# OCS (Open Chat Studio) Integration Plan

**Date:** 2026-03-26 (revised 2026-03-27)
**Branch:** `feature/ocs-integration`
**Goal:** Add OCS as a third workspace provider in Scout, enabling users to query chatbot session data.

## Revision Notes

Revised based on [PR #116 review](https://github.com/dimagi-rad/scout/pull/116#issuecomment-4139697208). Key changes:
- **Dropped `OCSTeamCredential`** — uses existing `TenantCredential` + `credential_resolver.py` pipeline (#1)
- **Dropped `ocs_team` FK on Tenant** — uses `OCSTeam` as a standalone group record linked via `team_slug` stored on Tenant metadata (#2)
- **Resolved message_id** — use deterministic composite key `{session_id}:{message_index}` with stable ordering (#3)
- **Added PROVIDER_TOKEN_URLS entry** for OCS token refresh (#4)
- **Added all missing integration points** — credential_resolver, me_view, PROVIDER_DISPLAY, apps.py, admin registration (#5-12)
- **Added chatbot lifecycle, incremental sync, rate limiting, and negative-path testing** (#13-20)

---

## Context

Scout currently supports two providers: **CommCare HQ** (domains) and **CommCare Connect** (opportunities). This plan adds **OCS** as a third provider, following the same architectural patterns.

### OCS Concepts Mapped to Scout

| OCS Concept | Scout Concept | Notes |
|-------------|---------------|-------|
| Team | Auth/credential scope | Selected during OAuth consent screen; stored as `OCSTeam` |
| Experiment (Chatbot) | Tenant → Workspace | Each chatbot = one entry in workspace dropdown |
| Session | Materialized data (raw_sessions) | Conversation instance with a chatbot |
| Message | Materialized data (raw_messages) | Individual message within a session |
| File | Materialized data (raw_files) | Attachment on a message |

### Key Design Decision: Chatbot-as-Tenant

Each OCS chatbot becomes its own `Tenant` (provider=`"ocs"`, external_id=experiment UUID). The OAuth token is team-scoped via allauth's `SocialToken`, so multiple chatbot tenants share one token naturally. An `OCSTeam` model tracks team membership for experiment discovery, but credentials flow through the existing `TenantCredential` → `credential_resolver.py` pipeline.

---

## Phase 1: Data Model

### 1.1 Add `"ocs"` provider choice

**File:** `apps/users/models.py`

```python
PROVIDER_CHOICES = [
    ("commcare", "CommCare HQ"),
    ("commcare_connect", "CommCare Connect"),
    ("ocs", "Open Chat Studio"),
]
```

### 1.2 New model: `OCSTeam`

**File:** `apps/users/models.py`

Tracks which OCS teams a user has authorized. Used during experiment discovery to know which team's experiments to fetch. Does NOT hold credentials — those go through `SocialToken` (OAuth) or `TenantCredential` (API key).

```python
class OCSTeam(models.Model):
    """Tracks a user's authorized OCS team for experiment discovery."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_slug = models.CharField(max_length=255)
    team_name = models.CharField(max_length=255)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ocs_teams"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["user", "team_slug"]]

    def __str__(self):
        return f"OCSTeam({self.team_name} [{self.team_slug}] for {self.user})"
```

Register in `apps/users/admin.py`:
```python
@admin.register(OCSTeam)
class OCSTeamAdmin(admin.ModelAdmin):
    list_display = ("team_name", "team_slug", "user", "created_at")
    search_fields = ("team_name", "team_slug", "user__email")
```

### 1.3 Credential flow (existing pipeline, no new models)

**OAuth (primary path):**
- OCS OAuth token is stored in `allauth.socialaccount.SocialToken` automatically
- Each OCS chatbot Tenant gets a `TenantCredential(credential_type=OAUTH)` via the existing resolver pattern
- `credential_resolver.py` resolves the token by matching `account__provider__startswith="ocs"`

**API key (secondary path):**
- Stored in `TenantCredential(credential_type=API_KEY, encrypted_credential=...)` — same as CommCare API keys
- Each chatbot Tenant gets its own `TenantCredential` pointing to the same encrypted key

This keeps a **single credential resolution path** through `credential_resolver.py`.

### 1.4 Migration

```bash
uv run python manage.py makemigrations users
```

---

## Phase 2: OAuth Provider (allauth)

### 2.1 Create OCS OAuth adapter

**New directory:** `apps/users/providers/ocs/`

Files to create:
- `__init__.py` — empty or with `default_app_config`
- `apps.py` — `OCSProviderConfig(AppConfig)` with `name = "apps.users.providers.ocs"`
- `provider.py` — `OCSProvider(OAuth2Provider)` with:
  - `id = "ocs"`
  - `name = "Open Chat Studio"`
  - `oauth2_adapter_class = OCSOAuth2Adapter`
  - `get_default_scope()` → `["chatbots:read", "sessions:read", "files:read", "openid"]`
  - `extract_uid(data)` from userinfo response
  - `extract_common_fields(data)` for email/name
- `views.py` — `OCSOAuth2Adapter(OAuth2Adapter)` with:
  - `authorize_url` = `f"{settings.OCS_URL}/o/authorize/"`
  - `access_token_url` = `f"{settings.OCS_URL}/o/token/"`
  - `profile_url` = `f"{settings.OCS_URL}/o/userinfo/"`
  - PKCE support via `SOCIALACCOUNT_PROVIDERS` config
- `urls.py` — `default_urlpatterns(OCSProvider)` (allauth auto-discovers via `config/urls.py` include of `allauth.urls`)

**Settings additions** (`config/settings/base.py`):
```python
OCS_URL = env("OCS_URL", default="https://www.openchatstudio.com")

INSTALLED_APPS += ["apps.users.providers.ocs"]

SOCIALACCOUNT_PROVIDERS["ocs"] = {
    "OAUTH_PKCE_ENABLED": True,
}
```

### 2.2 Update auth_views.py integration points

**File:** `apps/users/auth_views.py`

```python
# Add to PROVIDER_DISPLAY
PROVIDER_DISPLAY = {
    ...
    "ocs": "Open Chat Studio",
}

# Add to PROVIDER_TOKEN_URLS
PROVIDER_TOKEN_URLS = {
    ...
    "ocs": f"{settings.OCS_URL}/o/token/",
}
```

### 2.3 API key auth support

**File:** `apps/users/api/views.py` (or new endpoint)

Add endpoint for users to submit an OCS API key:
- Accepts: `api_key`, `team_slug`, `ocs_url` (optional)
- Validates by calling `GET {ocs_url}/api/experiments/` with `X-api-key` header
- On success: creates `OCSTeam`, triggers experiment discovery, creates `TenantCredential(API_KEY)` per chatbot tenant

---

## Phase 3: Tenant Resolution (Experiment Discovery)

### 3.1 New resolver function

**File:** `apps/users/services/tenant_resolution.py`

Follows the same signature pattern as existing resolvers (`user, access_token: str`):

```python
class OCSAuthError(Exception):
    """Raised when OCS returns a 401/403 during experiment resolution."""

def resolve_ocs_experiments(user, access_token: str) -> list[TenantMembership]:
    """Fetch all experiments for the user's OCS team and create Tenant + Workspace per chatbot."""
    ocs_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com")
    experiments = _fetch_all_ocs_experiments(access_token, ocs_url)
    memberships = []
    for exp in experiments:
        tenant, _ = Tenant.objects.update_or_create(
            provider="ocs",
            external_id=str(exp["id"]),
            defaults={"canonical_name": exp["name"]},
        )
        tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
        TenantCredential.objects.get_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)
    return memberships
```

### 3.2 Experiment list pagination

```python
def _fetch_all_ocs_experiments(access_token: str, base_url: str) -> list[dict]:
    """Paginate through GET /api/experiments/ using cursor pagination."""
    results = []
    url = f"{base_url.rstrip('/')}/api/experiments/"
    while url:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if resp.status_code in (401, 403):
            raise OCSAuthError(f"OCS returned {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        url = data.get("next")  # cursor pagination next URL
    return results
```

### 3.3 Hook into signals

**File:** `apps/users/signals.py`

Add OCS branch to `resolve_tenant_on_social_login()`:
```python
elif provider == "ocs":
    try:
        from apps.users.services.tenant_resolution import resolve_ocs_experiments
        resolve_ocs_experiments(sociallogin.user, token.token)
    except Exception:
        logger.warning("Failed to resolve OCS experiments after OAuth", exc_info=True)
```

### 3.4 Update me_view for lazy resolution

**File:** `apps/users/auth_views.py`

Add OCS to the lazy provider resolution chain in `me_view()`:
```python
# Add _get_ocs_token helper in apps/users/views.py
_try_resolve_provider(user, _get_ocs_token, resolve_ocs_experiments, "OCS")
```

### 3.5 Update credential_resolver.py

**File:** `apps/users/services/credential_resolver.py`

Add OCS branch to the OAuth token lookup:
```python
provider = membership.tenant.provider
if provider == "commcare_connect":
    token_obj = SocialToken.objects.filter(
        account__user=membership.user,
        account__provider__startswith="commcare_connect",
    ).first()
elif provider == "ocs":
    token_obj = SocialToken.objects.filter(
        account__user=membership.user,
        account__provider__startswith="ocs",
    ).first()
else:
    # CommCare HQ (existing logic)
    token_obj = (
        SocialToken.objects.filter(...)
        .exclude(...)
        .first()
    )
```

### 3.6 Workspace auto-creation

The existing `auto_create_workspace_on_membership` signal handler creates one Workspace per TenantMembership — works as-is.

### 3.7 Tenant lifecycle management

When `resolve_ocs_experiments` runs, it should also handle deletions:
- After fetching current experiments, compare with existing OCS tenants for this user
- Mark orphaned tenants (chatbots deleted in OCS) — either soft-delete or archive
- Do NOT auto-delete workspaces that may contain materialized data; flag for user review

---

## Phase 4: MCP Data Loaders

### 4.1 OCS base loader

**New file:** `mcp_server/loaders/ocs_base.py`

```python
class OCSAuthError(Exception):
    """Raised when OCS returns a 401 or 403 response."""

class OCSBaseLoader:
    """Base class for OCS API loaders."""
    DEFAULT_BASE_URL = "https://www.openchatstudio.com"
    HTTP_TIMEOUT = (10, 120)  # (connect, read)

    def __init__(self, experiment_id, credential, base_url=None):
        self.experiment_id = experiment_id
        self.base_url = base_url or self._get_base_url()
        self._session = requests.Session()
        if credential["type"] == "api_key":
            self._session.headers["X-api-key"] = credential["value"]
        else:
            self._session.headers["Authorization"] = f"Bearer {credential['value']}"

    def _get(self, url, params=None):
        resp = self._session.get(url, params=params, timeout=self.HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise OCSAuthError(f"OCS auth failed: HTTP {resp.status_code}")
        if resp.status_code == 429:
            # Rate limited — log and raise
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise OCSRateLimitError(f"Rate limited, retry after {retry_after}s")
        resp.raise_for_status()
        return resp
```

### 4.2 Session loader

**New file:** `mcp_server/loaders/ocs_sessions.py`

- `OCSSessionLoader(OCSBaseLoader)`
- List endpoint: `GET /api/sessions/?experiment={experiment_id}&page_size=500`
- Cursor-based pagination
- The sessions list endpoint returns messages inline per session (no N+1 detail calls needed — verify during implementation; if not, batch with controlled concurrency)
- `load_pages()` yields lists of session dicts with embedded messages

**Incremental sync strategy:**
- Filter sessions by `ordering=-created_at` and stop pagination when reaching sessions already in the schema (compare `created_at` with latest in `raw_sessions`)
- Full re-sync available via explicit user action

### 4.3 Experiment metadata loader

**New file:** `mcp_server/loaders/ocs_metadata.py`

- `OCSMetadataLoader(OCSBaseLoader)`
- Fetches experiment detail: `GET /api/experiments/{experiment_id}/`
- Returns: experiment name, versions, version descriptions

### 4.4 File loader

**New file:** `mcp_server/loaders/ocs_files.py`

- `OCSFileLoader(OCSBaseLoader)`
- Extracts file references from session message metadata/attachments
- Stores file metadata (id, name, content_type, size) — not binary content
- Content download: `GET /api/files/{id}/content` (on-demand, not during materialization)

### 4.5 Rate limiting

All loaders implement:
- Respect `Retry-After` headers from 429 responses
- Configurable delay between pagination requests (default 0, increase if rate-limited)
- Exponential backoff on transient 5xx errors (max 3 retries)

---

## Phase 5: Pipeline and Materialization

### 5.1 Pipeline config

**New file:** `pipelines/ocs_sync.yml`

```yaml
pipeline: ocs_sync
description: "Sync data from Open Chat Studio"
version: "1.0"
provider: ocs

sources:
  - name: sessions
    description: "Chat sessions with full message history"
  - name: files
    description: "File attachments from chat messages"

metadata_discovery:
  description: "Fetch experiment detail and version info from OCS API"

relationships:
  - from_table: raw_messages
    from_column: session_id
    to_table: raw_sessions
    to_column: session_id
    description: "Messages belong to a session"
  - from_table: raw_files
    from_column: message_id
    to_table: raw_messages
    to_column: message_id
    description: "Files are attached to messages"
```

### 5.2 Materializer updates

**File:** `mcp_server/services/materializer.py`

Add OCS imports and branches to:

**`_run_discover_phase()`:**
```python
elif pipeline.provider == "ocs":
    loader = OCSMetadataLoader(
        experiment_id=tenant_membership.tenant.external_id,
        credential=credential,
    )
```

**`_load_source()`:**
```python
elif provider == "ocs":
    return _load_ocs_source(source_name, tenant_membership, credential, schema_name, conn)
```

**New function `_load_ocs_source()`:**
```python
def _load_ocs_source(source_name, tenant_membership, credential, schema_name, conn):
    exp_id = tenant_membership.tenant.external_id
    loader_map = {
        "sessions": (OCSSessionLoader, _write_ocs_sessions),
        "files": (OCSFileLoader, _write_ocs_files),
    }
    loader_cls, writer_fn = loader_map[source_name]
    loader = loader_cls(experiment_id=exp_id, credential=credential)
    return writer_fn(loader.load_pages(), schema_name, conn)
```

### 5.3 Table writers

**`raw_sessions` table** (uses TIMESTAMPTZ for proper time queries):
```sql
CREATE TABLE {schema}.raw_sessions (
    session_id TEXT PRIMARY KEY,
    experiment_id TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    status TEXT,
    tags JSONB DEFAULT '[]'::jsonb,
    state JSONB DEFAULT '{}'::jsonb
)
```

**`raw_messages` table:**

Message IDs use a deterministic composite key: `{session_id}:{zero_padded_index}` where index is the message's position in the session's message array. This is stable across re-syncs as long as OCS preserves message ordering (messages are append-only within a session).

```sql
CREATE TABLE {schema}.raw_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES {schema}.raw_sessions(session_id),
    message_index INTEGER NOT NULL,
    role TEXT,
    content TEXT,
    created_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb,
    tags JSONB DEFAULT '[]'::jsonb
)
```

**`raw_files` table:**
```sql
CREATE TABLE {schema}.raw_files (
    file_id TEXT PRIMARY KEY,
    message_id TEXT,
    name TEXT,
    content_type TEXT,
    size INTEGER
)
```

---

## Phase 6: Frontend

### 6.1 Connection UI

The existing connections/settings page needs an "Add OCS Connection" option:
- "Connect with OCS" button (triggers OAuth flow)
- "Use API Key" alternative (form for key + team slug entry)
- After auth: show team name and count of discovered chatbots

### 6.2 Workspace dropdown

No structural changes needed. Each chatbot auto-populates via the existing signal.

**UX consideration for chatbot proliferation (#13):** If a team has many chatbots (50+), the dropdown becomes unwieldy. Options to address:
- Add search/filter to the workspace dropdown (general UX improvement, not OCS-specific)
- Group OCS workspaces under a "OCS: {team_name}" header in the dropdown
- Defer to a follow-up UX pass if volume becomes a real issue

### 6.3 Provider branding

Add to `PROVIDER_DISPLAY`: "Open Chat Studio". Add OCS logo/icon for workspace dropdown items.

---

## Phase 7: Settings and Configuration

### 7.1 Environment variables

Add to `.env.example`:
```
OCS_URL=https://www.openchatstudio.com
OCS_OAUTH_CLIENT_ID=
OCS_OAUTH_CLIENT_SECRET=
```

### 7.2 Django settings

**File:** `config/settings/base.py`

```python
OCS_URL = env("OCS_URL", default="https://www.openchatstudio.com")
```

---

## Implementation Order

| Step | Phase | Description | Dependencies |
|------|-------|-------------|--------------|
| 1 | 1 | Data model (provider choice + OCSTeam + migration) | None |
| 2 | 7 | Settings and env vars | None |
| 3 | 2.1 | OAuth provider (allauth adapter, apps.py, urls.py) | Step 1, 2 |
| 4 | 2.2 | auth_views.py updates (PROVIDER_DISPLAY, PROVIDER_TOKEN_URLS) | Step 3 |
| 5 | 3 | Tenant resolution + credential_resolver + signals + me_view | Step 3, 4 |
| 6 | 4 | MCP data loaders (base, sessions, metadata, files) | Step 1 |
| 7 | 5 | Pipeline config + materializer updates + table writers | Step 6 |
| 8 | 6 | Frontend connection UI + provider branding | Step 4, 5 |
| 9 | 2.3 | API key auth endpoint | Step 5 |

Steps 1-2 are parallel. Steps 6-7 can run in parallel with steps 3-5.

---

## Testing Strategy

### Happy-path tests
- Unit tests for each loader (mock OCS API responses with realistic payloads)
- Unit tests for tenant resolution (mock experiment list, verify Tenant/Workspace creation)
- Unit tests for credential resolution — verify OCS branch in `credential_resolver.py` returns correct token, and that existing CommCare/Connect resolution is not broken
- Integration test for full pipeline: experiment discovery → materialization → query

### Negative-path tests
- Invalid/expired API key → verify `OCSAuthError` raised, no partial state created
- Expired OAuth token → verify token refresh via `PROVIDER_TOKEN_URLS["ocs"]`
- OCS API 4xx/5xx responses → verify graceful failure, no data corruption
- Rate limit (429) responses → verify backoff and retry
- Partial experiment list (pagination error mid-stream) → verify partial results handled
- Chatbot deleted in OCS → verify tenant lifecycle cleanup

### Manual QA
- OAuth flow end-to-end (requires OCS OAuth app registration)
- Team selection on OCS consent screen → correct experiments discovered
- Workspace dropdown populated with chatbot names
- Materialization produces queryable session/message data

---

## Resolved Questions

1. **Message IDs:** OCS messages don't have individual IDs. We use deterministic composite keys: `{session_id}:{zero_padded_index}`. This is stable because messages are append-only within a session — existing messages never reorder.

2. **Timestamps:** All timestamp columns use `TIMESTAMPTZ` instead of `TEXT` for proper time-range queries and indexing.

3. **Credential resolution:** Uses the existing `TenantCredential` → `credential_resolver.py` pipeline. No parallel credential system.

4. **Token refresh:** OCS entry added to `PROVIDER_TOKEN_URLS` so `_resolve_oauth_credential()` handles expired tokens during materialization.

## Remaining Open Questions

1. **OCS team info from OAuth:** Does the OCS `/o/userinfo/` endpoint return team slug/name? If not, we need to extract it from the token response or call another endpoint. This determines whether `OCSTeam` can be auto-populated on OAuth or requires a separate step.

2. **Session list message inclusion:** Does `GET /api/sessions/` include full message history inline, or does it require per-session detail calls? This determines whether the N+1 concern (#16) applies.

3. **File attachment structure:** Are file references embedded in message metadata/attachments, or is there a separate list endpoint? This determines the file loader implementation.

4. **Rate limits for bulk endpoints:** The 30-second poll minimum applies to chat polling. Do list/detail endpoints have separate limits? Determines whether we need request throttling during materialization.
