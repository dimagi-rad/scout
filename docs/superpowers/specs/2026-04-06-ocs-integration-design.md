# OCS (Open Chat Studio) Integration Design

**Date:** 2026-04-06
**Status:** Draft

## Overview

Add Open Chat Studio (OCS) as a third data source in Scout, alongside CommCare and CommCare Connect. Users will OAuth into OCS, select a team, and Scout will create one workspace per chatbot in that team. Materializing a workspace pulls that chatbot's sessions, messages, and participants into PostgreSQL for SQL-based querying by the Scout AI agent.

## OCS API Summary

- **Base URL:** Configurable via `OCS_URL` env var (default `https://www.openchatstudio.com`)
- **Auth:** OAuth2 with PKCE. Tokens are scoped to the team the user selects during the OAuth consent screen.
- **Key endpoints:**
  - `GET /api/experiments/` — List chatbots (cursor-paginated)
  - `GET /api/experiments/{id}/` — Retrieve single chatbot
  - `GET /api/sessions/` — List sessions, filterable by `experiment` (cursor-paginated)
  - `GET /api/sessions/{id}/` — Retrieve session with embedded messages
  - `GET /api/files/{id}/content` — Download file attachment
- **Pagination:** Cursor-based
- **Data model:** Team > Experiments (chatbots) > Sessions > Messages. Participants are embedded in sessions.
- **No dedicated endpoints for:** listing teams, listing participants, bulk export, SQL/database access

## Tenant Mapping

| Provider | Auth scope | Tenant maps to | external_id | Workspace shows |
|---|---|---|---|---|
| CommCare | User's domains | Domain | domain name | Domain name |
| Connect | User's orgs | Opportunity | opportunity ID | Opportunity name |
| **OCS** | **User-selected team** | **Chatbot** | **experiment UUID** | **Chatbot name** |

One OCS team = one OAuth token. One chatbot = one tenant = one workspace. After OAuth, `resolve_ocs_chatbots()` calls `GET /api/experiments/` and creates a tenant per chatbot returned.

### Switching Teams

OCS tokens are team-scoped. To switch teams, the user disconnects OCS from the Connected Accounts page and re-connects, selecting a different team during the OAuth consent screen. This reuses the existing disconnect/reconnect flow with no new code.

## OAuth Provider

New allauth provider at `apps/users/providers/ocs/`, following the `commcare_connect` pattern.

### Provider class: `OCSProvider(OAuth2Provider)`

- `id = "ocs"`
- `name = "Open Chat Studio"`
- Scopes: `["chatbots:read", "sessions:read", "files:read", "openid"]`
- `extract_uid(data)` — returns `sub` from `/o/userinfo/` response
- `extract_common_fields(data)` — returns whatever `/o/userinfo/` provides (may be minimal)

### Adapter class: `OCSOAuth2Adapter(OAuth2Adapter)`

- `authorize_url = f"{settings.OCS_URL}/o/authorize/"`
- `access_token_url = f"{settings.OCS_URL}/o/token/"`
- `profile_url = f"{settings.OCS_URL}/o/userinfo/"`
- `complete_login()` — fetches userinfo, returns `sociallogin_from_response()`
- PKCE enabled: `SOCIALACCOUNT_PROVIDERS["ocs"] = {"OAUTH_PKCE_ENABLED": True}`

### Environment Variables

- `OCS_URL` — Base URL (default `https://www.openchatstudio.com`)
- `OCS_OAUTH_CLIENT_ID` — OAuth app client ID
- `OCS_OAUTH_CLIENT_SECRET` — OAuth app client secret

## Tenant Resolution

New function `resolve_ocs_chatbots(user, access_token)` in `apps/users/services/tenant_resolution.py`, following the `resolve_connect_opportunities()` pattern:

1. Calls `GET /api/experiments/` with Bearer token, paginates through all results
2. For each chatbot: upserts `Tenant(provider="ocs", external_id=experiment_uuid, canonical_name=chatbot_name)`
3. Creates `TenantMembership` + `TenantCredential(OAUTH)` for each
4. Returns `list[TenantMembership]`

Since the token is team-scoped, all returned chatbots belong to the user's selected team.

New `OCSAuthError` exception class, following `CommCareAuthError` / `ConnectAuthError`.

### Integration Points

- `signals.py` — `resolve_tenant_on_social_login()` gets `elif provider == "ocs":` branch calling `resolve_ocs_chatbots()`
- `auth_views.py` — `me_view()` gets `_try_resolve_provider()` call for OCS
- `views.py` — `tenant_list_view()` gets OCS refresh block with cache key `tenant_refresh:{user.id}:ocs`
- `views.py` — New `_get_ocs_token()` helper alongside `_get_commcare_token()` and `_get_connect_token()`

## Pipeline

### `pipelines/ocs_sync.yml`

```yaml
pipeline: ocs_sync
description: "Sync data from Open Chat Studio"
version: "1.0"
provider: ocs

sources:
  - name: experiments
    description: "Chatbot definitions and versions"
  - name: sessions
    description: "Chat sessions with timestamps, tags, and participant info"
  - name: messages
    description: "Individual messages within sessions"
  - name: participants
    description: "Unique participants extracted from sessions"

metadata_discovery:
  description: "Fetch experiment detail from OCS API"

relationships:
  - from_table: raw_sessions
    from_column: experiment_id
    to_table: raw_experiments
    to_column: experiment_id
    description: "Sessions belong to a chatbot"
  - from_table: raw_messages
    from_column: session_id
    to_table: raw_sessions
    to_column: session_id
    description: "Messages belong to a session"
  - from_table: raw_sessions
    from_column: participant_identifier
    to_table: raw_participants
    to_column: identifier
    description: "Sessions reference the participant"
```

## Loaders

All in `mcp_server/loaders/`, following `ConnectBaseLoader` pattern.

### `ocs_base.py` — `OCSBaseLoader`

- Constructor: `(experiment_id: str, credential: dict, base_url: str | None)`
- `requests.Session` with `Authorization: Bearer {token}`
- Base URL from `settings.OCS_URL`
- Cursor-based pagination helper (OCS uses cursor pagination, not offset)
- `OCSAuthError` on 401/403

### `ocs_experiments.py` — `OCSExperimentLoader`

- `GET /api/experiments/{id}/` — fetches the single chatbot for this tenant
- Returns: `experiment_id`, `name`, `url`, `version_number`

### `ocs_sessions.py` — `OCSSessionLoader`

- `GET /api/sessions/?experiment={id}` — cursor-paginated
- Returns: `session_id`, `experiment_id`, `participant_identifier`, `participant_platform`, `created_at`, `updated_at`, `tags`

### `ocs_messages.py` — `OCSMessageLoader`

- For each session, `GET /api/sessions/{id}/` to get embedded messages
- Flattens into rows: `session_id`, `message_index`, `role`, `content`, `created_at`, `metadata`, `tags`
- Primary key: composite `{session_id}:{message_index}` (messages have no unique ID in the API)
- This is an N+1 pattern — one detail call per session. Acceptable given typical chatbot session volumes.

### `ocs_participants.py` — `OCSParticipantLoader`

- No dedicated OCS endpoint for listing participants
- Extracts unique participants from session list data: `identifier`, `platform`, `remote_id`
- Deduplicates by `identifier`

### `ocs_metadata.py` — `OCSMetadataLoader`

- Fetches experiment detail for `TenantMetadata` storage
- Used by the discover phase of materialization

## Materialized Tables

| Table | Primary Key | Columns |
|---|---|---|
| `raw_experiments` | `experiment_id` (TEXT, UUID) | `name`, `url`, `version_number` |
| `raw_sessions` | `session_id` (TEXT, UUID) | `experiment_id`, `participant_identifier`, `participant_platform`, `created_at`, `updated_at`, `tags` (JSONB) |
| `raw_messages` | `message_id` (TEXT, composite) | `session_id`, `message_index`, `role`, `content`, `created_at`, `metadata` (JSONB), `tags` (JSONB) |
| `raw_participants` | `identifier` (TEXT) | `platform`, `remote_id` |

## Materializer Changes

In `mcp_server/services/materializer.py`:

- New `_load_ocs_source()` dispatch function, following `_load_connect_source()` pattern with a `loader_map` dict
- `_load_source()` gets `elif provider == "ocs":` branch
- `_run_discover_phase()` gets `elif pipeline.provider == "ocs":` branch using `OCSMetadataLoader`
- New table writers: `_write_ocs_experiments()`, `_write_ocs_sessions()`, `_write_ocs_messages()`, `_write_ocs_participants()`
- New INSERT SQL constants for each table

## Registrations

| Location | Change |
|---|---|
| `apps/users/models.py` | Add `("ocs", "Open Chat Studio")` to `PROVIDER_CHOICES` |
| `apps/users/auth_views.py` | Add to `PROVIDER_DISPLAY` and `PROVIDER_TOKEN_URLS` |
| `config/settings/base.py` | Add `OCS_URL` env var, `apps.users.providers.ocs` to `INSTALLED_APPS`, `SOCIALACCOUNT_PROVIDERS["ocs"]` |
| `apps/users/migrations/` | New migration for `PROVIDER_CHOICES` update |

## What's NOT Changing

- Workspace model / WorkspaceTenant / WorkspaceMembership — auto-created by existing `post_save` signal
- Schema provisioning — `SchemaManager.provision()` is provider-agnostic
- MCP server context resolution — `load_workspace_context()` / `load_tenant_context()` work off schema name
- Transformation pipeline — dbt stages work off tenant schema
- Credential resolver — existing OAuth path via `SocialToken` works as-is
- Frontend — no OCS-specific UI. OCS appears automatically in OAuth Providers list and Workspace dropdown once the `SocialApp` is configured.

## File Change Summary

| Layer | Files | Change |
|---|---|---|
| Provider | `apps/users/providers/ocs/` (new) | `__init__.py`, `provider.py`, `views.py`, `apps.py`, `urls.py` |
| Models | `apps/users/models.py` | Add `("ocs", "Open Chat Studio")` to `PROVIDER_CHOICES` |
| Auth views | `apps/users/auth_views.py` | Add OCS to `PROVIDER_DISPLAY`, `PROVIDER_TOKEN_URLS` |
| User views | `apps/users/views.py` | Add `_get_ocs_token()`, OCS refresh block in `tenant_list_view()` |
| Tenant resolution | `apps/users/services/tenant_resolution.py` | Add `resolve_ocs_chatbots()`, `OCSAuthError` |
| Signals | `apps/users/signals.py` | Add `elif provider == "ocs":` branch |
| Settings | `config/settings/base.py` | Add `OCS_URL`, `INSTALLED_APPS` entry, `SOCIALACCOUNT_PROVIDERS` entry |
| Pipeline | `pipelines/ocs_sync.yml` (new) | Pipeline definition |
| Loaders | `mcp_server/loaders/ocs_*.py` (new) | `ocs_base`, `ocs_experiments`, `ocs_sessions`, `ocs_messages`, `ocs_participants`, `ocs_metadata` |
| Materializer | `mcp_server/services/materializer.py` | `_load_ocs_source()`, OCS writers, discover branch |
| Migration | `apps/users/migrations/` (new) | `PROVIDER_CHOICES` update |
