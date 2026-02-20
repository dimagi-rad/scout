# API Key Authentication Design

**Date:** 2026-02-20
**Status:** Approved
**Context:** Local development without OAuth is painful. This adds support for CommCare API keys as an alternative credential type, alongside a username/password login path for Scout itself.

---

## Problem

OAuth requires a registered application, redirect URIs, and a live callback — hard to set up for local dev. CommCare natively supports `ApiKey username:apikey` authentication, which is simpler to obtain and use.

---

## Goals

- Let users log into Scout with email/password (no OAuth required)
- Let users provide a CommCare API key + username + domain as an alternative to OAuth for data materialization
- Keep the OAuth path fully intact for production use
- Design the credential storage generically enough to support CommCare Connect (and other providers) in the future

---

## Data Model

New model `TenantCredential` in `apps/users/`, one-to-one with `TenantMembership`:

```python
class TenantCredential(models.Model):
    OAUTH   = "oauth"
    API_KEY = "api_key"
    TYPE_CHOICES = [(OAUTH, "OAuth Token"), (API_KEY, "API Key")]

    id                   = UUIDField(primary_key=True)
    tenant_membership    = OneToOneField(TenantMembership, related_name="credential")
    credential_type      = CharField(max_length=20, choices=TYPE_CHOICES)
    encrypted_credential = CharField(max_length=2000, blank=True)
    # Fernet-encrypted opaque string. Format is provider-specific:
    #   CommCare HQ:      "username:apikey"
    #   Bearer providers: plain key string
    # Empty for credential_type == "oauth" (token lives in allauth SocialToken)

    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
```

Uses the same `DB_CREDENTIAL_KEY` Fernet key as database credential encryption. No data migration needed (no existing data).

When OAuth completes, a post-save signal on `TenantMembership` auto-creates `TenantCredential(type="oauth")` so both paths produce the same model shape.

---

## Credential Retrieval & MCP Flow

`apps/agents/mcp_client.py` currently exports `get_user_oauth_tokens()` returning `dict[str, str]`. This is renamed and extended:

```python
@dataclass
class ProviderCredential:
    credential_type: Literal["oauth", "api_key"]
    value: str  # decrypted: bearer token, "username:apikey", or plain key

async def get_user_credentials(user) -> dict[str, ProviderCredential]:
    # For each TenantMembership with a TenantCredential:
    #   type == "oauth"    → fetch SocialToken.token (decrypted by existing adapter)
    #   type == "api_key"  → Fernet-decrypt encrypted_credential
```

This dict flows through the existing LangGraph config path (renamed from `oauth_tokens` to `credentials`). The MCP server's `extract_oauth_tokens` becomes `extract_credentials`, returning the same dict.

Loaders construct the auth header from the credential type:

```python
# mcp_server/loaders/commcare_cases.py
def _auth_header(self) -> str:
    if self.credential.credential_type == "api_key":
        return f"ApiKey {self.credential.value}"   # value = "username:apikey"
    return f"Bearer {self.credential.value}"
```

**Blast radius:** `mcp_client.py`, `chat/views.py`, `mcp_server/auth.py`, `mcp_server/loaders/commcare_cases.py`. Everything else unchanged.

---

## Scout Authentication

Enable allauth email/password login via settings in `config/settings/base.py`:

```python
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_AUTHENTICATION_METHOD = "email"
ACCOUNT_EMAIL_VERIFICATION = env("ACCOUNT_EMAIL_VERIFICATION", default="none")
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
```

`ACCOUNT_EMAIL_VERIFICATION` defaults to `"none"` (suitable for local dev) and can be set to `"mandatory"` in production via env var. No model changes required; allauth's `EmailAddress` model handles this.

Frontend additions:
- Login page with email/password fields + existing OAuth buttons
- Signup page with email/password/confirm fields

---

## Onboarding Wizard

Triggered for any user with no `TenantMembership`. Detected via `onboarding_complete` flag on `GET /api/auth/me/`:

```json
{ "onboarding_complete": false }
```

`onboarding_complete` is `true` when the user has at least one `TenantMembership` with a `TenantCredential`. No model field needed.

**Step 1 — Choose connection method:**
```
┌─────────────────────────────────────┐
│  Connect your CommCare data         │
│                                     │
│  [Connect with OAuth]               │
│  [Use an API Key]                   │
└─────────────────────────────────────┘
```

**Step 2a — OAuth path:** Redirects to existing CommCare OAuth flow. On success, allauth creates `SocialAccount` + `SocialToken` + `TenantMembership`; post-save signal creates `TenantCredential(type="oauth")`. User lands in app.

**Step 2b — API key path:** Form with:
- CommCare domain (e.g. `my-project`)
- CommCare username (email)
- API key (from CommCare → Settings → My Account → API Key)

On submit, `POST /api/auth/tenant-credentials/` creates `TenantMembership` + `TenantCredential(type="api_key")` atomically. User lands in app.

The wizard only appears until the user has at least one credential. Additional tenants can be added from a settings page later.

---

## API Endpoints

All in `apps/users/`.

### `GET /api/auth/me/`
Extend existing response:
```json
{ "onboarding_complete": false }
```

### `POST /api/auth/tenant-credentials/`
```json
// Request
{
  "provider": "commcare",
  "tenant_id": "my-project",
  "tenant_name": "My Project",
  "credential": "myemail@example.com:abc123apikey"
}
// Response: 201 with membership_id
```
Creates `TenantMembership` + `TenantCredential(type="api_key")` atomically. `credential` is Fernet-encrypted before storage.

### `DELETE /api/auth/tenant-credentials/{membership_id}/`
Removes `TenantCredential` (and `TenantMembership` if no other credentials exist).

### `GET /api/auth/tenant-credentials/`
```json
[
  {
    "membership_id": "uuid",
    "provider": "commcare",
    "tenant_id": "my-project",
    "tenant_name": "My Project",
    "credential_type": "api_key"
  }
]
```
Never returns the decrypted credential value.

---

## Files Changed

| Layer | Files |
|---|---|
| Models | `apps/users/models.py` |
| Migrations | One new migration for `TenantCredential` |
| Settings | `config/settings/base.py` |
| Services | `apps/users/adapters.py` (post-save signal), `apps/agents/mcp_client.py`, `mcp_server/auth.py`, `mcp_server/loaders/commcare_cases.py` |
| API | `apps/users/views.py`, `apps/users/serializers.py`, `apps/users/urls.py` |
| Frontend | Login page, signup page, onboarding wizard, `me` endpoint consumer |
| Tests | See below |

---

## Tests

- `test_api_key_credential_stored_encrypted` — raw DB value must not contain plaintext key
- `test_commcare_loader_uses_apikey_header` — mock HTTP, assert `Authorization: ApiKey ...`
- `test_commcare_loader_uses_bearer_header` — existing OAuth behaviour preserved
- `test_onboarding_complete_flag` — `false` with no memberships, `true` after credential POST
- `test_tenant_credential_post_creates_membership` — atomic creation test
