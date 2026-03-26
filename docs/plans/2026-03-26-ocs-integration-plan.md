# OCS (Open Chat Studio) Integration Plan

**Date:** 2026-03-26
**Branch:** `feature/ocs-integration`
**Goal:** Add OCS as a third workspace provider in Scout, enabling users to query chatbot session data.

## Context

Scout currently supports two providers: **CommCare HQ** (domains) and **CommCare Connect** (opportunities). This plan adds **OCS** as a third provider, following the same architectural patterns.

### OCS Concepts Mapped to Scout

| OCS Concept | Scout Concept | Notes |
|-------------|---------------|-------|
| Team | Auth/credential scope | Selected during OAuth consent screen |
| Experiment (Chatbot) | Tenant → Workspace | Each chatbot = one entry in workspace dropdown |
| Session | Materialized data (raw_sessions) | Conversation instance with a chatbot |
| Message | Materialized data (raw_messages) | Individual message within a session |
| File | Materialized data (raw_files) | Attachment on a message |

### Key Design Decision: Chatbot-as-Tenant

Each OCS chatbot becomes its own `Tenant` (provider=`"ocs"`, external_id=experiment UUID). The OAuth token is team-scoped, so multiple chatbot tenants share one team credential. A new `OCSTeam` model links chatbot tenants to their team-level credential.

---

## Phase 1: Data Model

### 1.1 Add `"ocs"` provider choice

**File:** `apps/users/models.py`

Add to `PROVIDER_CHOICES`:
```python
PROVIDER_CHOICES = [
    ("commcare", "CommCare HQ"),
    ("commcare_connect", "CommCare Connect"),
    ("ocs", "Open Chat Studio"),
]
```

### 1.2 New model: `OCSTeam`

**File:** `apps/users/models.py`

```python
class OCSTeam(models.Model):
    """Stores team-level OCS credentials shared across chatbot tenants."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_slug = models.CharField(max_length=255)
    team_name = models.CharField(max_length=255)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ocs_teams")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["user", "team_slug"]]
```

### 1.3 Add `ocs_team` FK to `Tenant`

**File:** `apps/users/models.py`

Add nullable FK on Tenant:
```python
ocs_team = models.ForeignKey(
    "OCSTeam", null=True, blank=True,
    on_delete=models.SET_NULL, related_name="tenants",
    help_text="For OCS tenants: links to the team holding the OAuth credential.",
)
```

### 1.4 Credential storage for OCSTeam

**File:** `apps/users/models.py`

```python
class OCSTeamCredential(models.Model):
    """OAuth or API key credential for an OCS team."""
    OAUTH = "oauth"
    API_KEY = "api_key"
    TYPE_CHOICES = [(OAUTH, "OAuth Token"), (API_KEY, "API Key")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ocs_team = models.OneToOneField(OCSTeam, on_delete=models.CASCADE, related_name="credential")
    credential_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    encrypted_credential = models.CharField(
        max_length=2000, blank=True,
        help_text="Fernet-encrypted API key. Empty for OAuth type.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

### 1.5 Migration

Generate migration for all model changes:
```bash
uv run python manage.py makemigrations users
```

---

## Phase 2: OAuth Provider (allauth)

### 2.1 Create OCS OAuth adapter

**New directory:** `apps/users/providers/ocs/`

Files to create:
- `__init__.py`
- `provider.py` — `OCSProvider(OAuth2Provider)` with:
  - `id = "ocs"`
  - `name = "Open Chat Studio"`
  - Scopes: `chatbots:read`, `sessions:read`, `sessions:write`, `files:read`, `openid`
  - `extract_uid()` from userinfo response
  - `extract_common_fields()` for email/name
- `views.py` — `OCSAdapter(OAuth2Adapter)` with:
  - `authorize_url = "{OCS_URL}/o/authorize/"`
  - `access_token_url = "{OCS_URL}/o/token/"`
  - `profile_url = "{OCS_URL}/o/userinfo/"`
  - PKCE support (code_challenge_method=S256)
- `urls.py` — Standard allauth OAuth URL patterns

**Settings additions** (`config/settings/base.py`):
```python
OCS_URL = env("OCS_URL", default="https://www.openchatstudio.com")
```

Add `"apps.users.providers.ocs"` to `INSTALLED_APPS`.

### 2.2 API key auth support

**File:** `apps/users/api/views.py` (or new endpoint)

Add endpoint for users to submit an OCS API key manually:
- Accepts: `api_key`, `ocs_url` (optional, defaults to production)
- Validates the key by calling `GET {ocs_url}/api/experiments/` with `X-api-key` header
- On success: creates `OCSTeam` + `OCSTeamCredential(API_KEY)` + triggers experiment discovery (Phase 3)

---

## Phase 3: Tenant Resolution (Experiment Discovery)

### 3.1 New resolver function

**File:** `apps/users/services/tenant_resolution.py`

```python
def resolve_ocs_experiments(user, credential, ocs_team):
    """Fetch all experiments for the team and create Tenant + Workspace per chatbot."""
    experiments = _fetch_all_ocs_experiments(credential, ocs_team)
    memberships = []
    for exp in experiments:
        tenant, _ = Tenant.objects.update_or_create(
            provider="ocs",
            external_id=str(exp["id"]),  # experiment UUID
            defaults={
                "canonical_name": exp["name"],
                "ocs_team": ocs_team,
            },
        )
        tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
        memberships.append(tm)
    return memberships
```

Note: OCS chatbot tenants do NOT get their own `TenantCredential`. Credential resolution for OCS tenants follows `tenant.ocs_team.credential` instead.

### 3.2 Experiment list pagination

**Helper in** `apps/users/services/tenant_resolution.py`:

```python
def _fetch_all_ocs_experiments(credential, ocs_team):
    """Paginate through GET /api/experiments/ using cursor pagination."""
    # Auth: X-api-key header (API key) or Authorization: Bearer (OAuth)
    # Follow cursor-based pagination until no next page
    # Returns list of {"id": uuid, "name": str, ...}
```

### 3.3 Hook into signals

**File:** `apps/users/signals.py`

Add OCS branch to `resolve_tenant_on_social_login()`:
```python
elif provider == "ocs":
    # Get or create OCSTeam from OAuth token (team info from userinfo endpoint)
    # Call resolve_ocs_experiments(user, credential, ocs_team)
```

### 3.4 Workspace auto-creation

The existing `auto_create_workspace_on_membership` signal handler already creates one Workspace per TenantMembership. Since each chatbot becomes its own Tenant, this works as-is. Each chatbot will appear in the workspace dropdown.

---

## Phase 4: MCP Data Loaders

### 4.1 OCS base loader

**New file:** `mcp_server/loaders/ocs_base.py`

```python
class OCSBaseLoader:
    """Base class for OCS API loaders."""
    DEFAULT_BASE_URL = "https://www.openchatstudio.com"

    def __init__(self, experiment_id, credential, base_url=None):
        self.experiment_id = experiment_id
        self.base_url = base_url or self._get_base_url()
        self._session = requests.Session()
        # Support both API key and OAuth
        if credential["type"] == "api_key":
            self._session.headers["X-api-key"] = credential["value"]
        else:
            self._session.headers["Authorization"] = f"Bearer {credential['value']}"
```

### 4.2 Session loader

**New file:** `mcp_server/loaders/ocs_sessions.py`

- `OCSSessionLoader(OCSBaseLoader)`
- Endpoint: `GET /api/sessions/?experiment={experiment_id}`
- Cursor-based pagination (page_size=500)
- `load_pages()` yields lists of session dicts, each including full message history from the session detail endpoint
- Returns: `{"session_id", "experiment_id", "created_at", "updated_at", "status", "tags", "state", "messages": [...]}`

### 4.3 Experiment metadata loader

**New file:** `mcp_server/loaders/ocs_metadata.py`

- `OCSMetadataLoader(OCSBaseLoader)`
- Fetches experiment detail: `GET /api/experiments/{experiment_id}/`
- Returns: experiment name, versions, version descriptions

### 4.4 File loader

**New file:** `mcp_server/loaders/ocs_files.py`

- `OCSFileLoader(OCSBaseLoader)`
- Extracts file references from session messages
- Fetches file metadata (not content) for the files table
- Content download: `GET /api/files/{id}/content` (on-demand, not during materialization)

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

Add OCS branch to:

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

**New function `_load_ocs_source()`** — dispatches to session/file loaders similar to `_load_connect_source()`.

### 5.3 Table writers

Add to `mcp_server/services/materializer.py`:

**`raw_sessions` table:**
```sql
CREATE TABLE {schema}.raw_sessions (
    session_id TEXT PRIMARY KEY,
    experiment_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    status TEXT,
    tags JSONB DEFAULT '[]'::jsonb,
    state JSONB DEFAULT '{}'::jsonb
)
```

**`raw_messages` table:**
```sql
CREATE TABLE {schema}.raw_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES {schema}.raw_sessions(session_id),
    role TEXT,
    content TEXT,
    created_at TEXT,
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

### 5.4 Credential resolution for OCS

**File:** `mcp_server/server.py` (or credential resolution utility)

When resolving credentials for an OCS tenant during materialization:
```python
if tenant.provider == "ocs" and tenant.ocs_team:
    cred = tenant.ocs_team.credential
    # Return {"type": cred.credential_type, "value": decrypted_value_or_oauth_token}
```

This replaces the normal `TenantCredential` lookup path for OCS tenants.

---

## Phase 6: Frontend

### 6.1 Connection UI

The existing connections/settings page needs an "Add OCS Connection" option alongside CommCare and Connect. This should:
- Show "Connect with OCS" button (triggers OAuth flow)
- Show "Use API Key" alternative (opens form for key entry)
- After auth: show which team was authorized and list of discovered chatbots

### 6.2 Workspace dropdown

No changes needed — chatbot workspaces auto-populate via the existing Tenant → Workspace signal. Each chatbot appears as a selectable workspace.

### 6.3 Provider branding

Add OCS logo/icon for workspace dropdown items and connection cards. Display "Open Chat Studio" as the provider label.

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
OCS_OAUTH_CLIENT_ID = env("OCS_OAUTH_CLIENT_ID", default="")
OCS_OAUTH_CLIENT_SECRET = env("OCS_OAUTH_CLIENT_SECRET", default="")
```

---

## Implementation Order

| Step | Phase | Description | Dependencies |
|------|-------|-------------|--------------|
| 1 | 1 | Data model changes + migration | None |
| 2 | 7 | Settings and env vars | None |
| 3 | 2 | OAuth provider (allauth adapter) | Step 1, 2 |
| 4 | 3 | Tenant resolution + experiment discovery | Step 1, 3 |
| 5 | 4 | MCP data loaders (base, sessions, metadata, files) | Step 1 |
| 6 | 5 | Pipeline config + materializer updates + table writers | Step 5 |
| 7 | 5.4 | Credential resolution for OCS in MCP server | Step 1, 6 |
| 8 | 6 | Frontend connection UI + provider branding | Step 3, 4 |
| 9 | 2.2 | API key auth endpoint | Step 1, 4 |

Steps 1-2 can be done in parallel. Steps 3-4 are sequential. Steps 5-6 can be done in parallel with 3-4.

---

## Testing Strategy

- **Unit tests** for each loader (mock OCS API responses)
- **Unit tests** for tenant resolution (mock experiment list)
- **Unit tests** for credential resolution (OCSTeam → credential lookup)
- **Integration test** for full pipeline: experiment discovery → materialization → query
- **Manual QA** for OAuth flow end-to-end (requires OCS OAuth app registration)

---

## Open Questions

1. **OCS team info from OAuth**: Does the OCS `/o/userinfo/` endpoint return team slug/name? If not, we may need to extract it from the token response or call another endpoint after auth.
2. **Message IDs**: OCS session detail returns messages as an array. Are individual messages assigned unique IDs, or do we need to generate synthetic IDs (e.g., `{session_id}_{index}`)?
3. **File discovery**: Are file references embedded in message content/metadata, or is there a separate endpoint to list files per session?
4. **Rate limits**: OCS documents 30-second minimum between poll requests. Do the list/detail endpoints have separate rate limits we need to respect during bulk materialization?
