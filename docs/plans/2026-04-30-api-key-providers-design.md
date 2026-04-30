# API Key Provider Generalization (CommCare + OCS)

**Date:** 2026-04-30
**Status:** Design approved, ready for implementation plan
**Branch:** `bdr/add-ocs-api-auth`

## Goal

Generalize the existing CommCare API-key authentication so that Open Chat Studio
(OCS) can also be connected via a personal API key. The primary motivation is
local-development ergonomics: contributors can pull in real data from production
OCS bots without configuring an OAuth client per laptop.

The abstraction should be designed to admit a third API-key provider (CommCare
Connect) without further refactoring.

## Non-goals

- OAuth changes for any provider.
- Onboarding wizard updates (it remains CommCare-only for now).
- Production rollout concerns: this is a local-dev convenience feature; the
  underlying `TenantCredential` table already supports both auth types.
- Feature flagging or backwards-compatibility shims for the API request shape —
  the only consumer of these endpoints is our own frontend, so the cutover
  happens in one PR.

## Existing landscape

The foundation is already in place:

- `TenantCredential` model has `OAUTH` and `API_KEY` `credential_type` choices.
- `apps/users/services/credential_resolver.py` returns a typed dict
  `{"type": "api_key" | "oauth", "value": str}` regardless of provider.
- `mcp_server/loaders/ocs_*.py` consume that dict and the OCS OAuth path is
  fully working.
- The `Connections` page already supports listing/adding/editing/deleting
  CommCare API-key connections.

What's missing:

- The POST `/api/auth/tenant-credentials/` endpoint hard-rejects any provider
  other than `"commcare"`.
- There is no `verify_ocs_credential()` equivalent to
  `verify_commcare_credential()`.
- `OCSBaseLoader.__init__` hardcodes `Authorization: Bearer <value>` and does
  not branch on `credential["type"]`.
- The Connections-page form is shaped for CommCare's `username:apikey` and
  hardcodes `provider: "commcare"`.

## Approach: provider-strategy abstraction

Rather than adding a parallel OCS code path next to the existing CommCare one,
introduce a small strategy registry now so the third provider (Connect API key)
will drop in cleanly.

### Backend: strategy interface

A new module `apps/users/services/api_key_providers/` exposing a
`CredentialProviderStrategy` base class and one implementation per provider:

```python
class TenantDescriptor(NamedTuple):
    external_id: str
    canonical_name: str


class FormField(TypedDict):
    key: str            # "domain", "username", "api_key", ...
    label: str          # human-readable
    type: str           # "text" | "password"
    required: bool
    editable_on_rotate: bool


class CredentialProviderStrategy:
    provider_id: str
    display_name: str
    form_fields: list[FormField]

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        """Serialize form fields into the opaque encrypted_credential string."""

    @classmethod
    async def verify_and_discover(
        cls, fields: dict[str, str]
    ) -> list[TenantDescriptor]:
        """Hit the provider API, verify credentials, return one or more
        tenants the credential grants access to. Raises
        CredentialVerificationError on failure."""

    @classmethod
    async def verify_for_tenant(
        cls, fields: dict[str, str], external_id: str
    ) -> None:
        """Used on PATCH (key rotation): verify the new credential still
        grants access to the existing tenant external_id. Raises on failure."""
```

Concrete implementations:

- **`CommCareStrategy`** — fields `domain`, `username`, `api_key`. Packs as
  `"username:api_key"`. `verify_and_discover` hits
  `GET /api/user_domains/v1/` with `Authorization: ApiKey username:key` and
  returns `[(domain, domain)]` after confirming the domain is in the response.
- **`OCSStrategy`** — single field `api_key`. Packs as the raw API key.
  `verify_and_discover` hits `GET /api/experiments/` with `X-api-key: <key>`,
  paginates via the `next` cursor, and returns one descriptor per experiment:
  `(experiment.id, experiment.name)`.

A registry `STRATEGIES: dict[str, type[CredentialProviderStrategy]] = {...}`
keyed by `provider_id`.

### Backend: loader auth headers

Loader auth headers stay where they are — `mcp_server/loaders/*_base.py`. The
loaders already receive the typed credential dict and do not need access to the
strategy registry. `OCSBaseLoader` gets a small change to dispatch on
`credential["type"]`:

```python
if credential.get("type") == "api_key":
    self._session.headers.update({"X-api-key": credential["value"]})
else:
    self._session.headers.update({"Authorization": f"Bearer {credential['value']}"})
```

`CommCareBaseLoader.build_auth_header()` already does this dispatch and is
unchanged.

### HTTP API surface

#### `GET /api/auth/api-key-providers/` (new)

Returns the registry shape so the frontend can render the dialog dynamically:

```json
[
  {
    "id": "commcare",
    "display_name": "CommCare HQ",
    "fields": [
      {"key": "domain",   "label": "Domain",   "type": "text",     "required": true,  "editable_on_rotate": false},
      {"key": "username", "label": "Username", "type": "text",     "required": true,  "editable_on_rotate": true},
      {"key": "api_key",  "label": "API Key",  "type": "password", "required": true,  "editable_on_rotate": true}
    ]
  },
  {
    "id": "ocs",
    "display_name": "Open Chat Studio",
    "fields": [
      {"key": "api_key", "label": "API Key", "type": "password", "required": true, "editable_on_rotate": true}
    ]
  }
]
```

#### `POST /api/auth/tenant-credentials/` (changed shape)

- **Old:** `{provider, tenant_id, tenant_name, credential}`
- **New:** `{provider, fields: {…}}`

Server flow:

1. Look up `STRATEGIES[provider]`; 400 if unknown.
2. Validate that all required fields per the strategy's `form_fields` are
   present.
3. `await strategy.verify_and_discover(fields)`. On failure, return 400 with
   the error message.
4. For each `TenantDescriptor`:
   - `aget_or_create` `Tenant(provider, external_id)` (do not overwrite an
     existing `canonical_name`).
   - `aget_or_create` `TenantMembership(user, tenant)`.
   - `aupdate_or_create` `TenantCredential` with the packed+encrypted string.
5. Wrap the loop in `transaction.atomic()` (via `sync_to_async`) so partial
   failure doesn't leave half-connected state.

Response:

```json
{"memberships": [{"membership_id": "…", "tenant_id": "…", "tenant_name": "…"}, …]}
```

Length is 1 for CommCare, N for OCS.

#### `PATCH /api/auth/tenant-credentials/<membership_id>/` (changed shape)

- **Old:** `{credential, tenant_name?}`
- **New:** `{fields: {…}}`

Server flow:

1. Fetch membership, get `tenant.provider`, look up strategy.
2. Render only fields with `editable_on_rotate: true` — UI enforces this; server
   ignores any other keys in `fields` defensively.
3. `await strategy.verify_for_tenant(fields, tm.tenant.external_id)`.
4. Pack + re-encrypt + save.

Note: PATCH does **not** re-discover. Adding/removing OCS experiments belongs in
a delete-then-add flow.

### Frontend

**Connections page restructure:**

- Rename the existing "Connected Domains" section to "API Key Connections"
  (the term "domain" is CommCare-coded).
- Replace the "Add Domain" button with a single "Add API Connection" button.

**`ApiConnectionDialog.tsx` (new):**

- On open (add mode): fetch `/api/auth/api-key-providers/`, render a
  provider radio group, then dynamic fields below based on the selected
  provider's schema.
- On open (edit mode): provider is locked (read from the row); render only
  fields with `editable_on_rotate: true`.
- Submit posts/patches the new shapes.
- After a successful add, refresh the connection list so users see N rows for
  one OCS paste.

`ConnectionsPage.tsx` shrinks: drop the CommCare-specific form-field state and
submit logic, delegate to the dialog. Rename `ApiKeyDomain` → `ApiKeyConnection`.

**`data-testid` attributes:**

- `api-connection-dialog`
- `api-connection-provider-{id}`
- `api-connection-field-{key}`
- `api-connection-submit`

## Testing

### Backend (pytest)

- `tests/test_api_key_strategies.py`
  - `CommCareStrategy.pack_credential` → `"username:apikey"`
  - `CommCareStrategy.verify_and_discover` mocks httpx for happy path / 401 /
    missing domain
  - `OCSStrategy.verify_and_discover` covers single page, multi-page pagination
    via `next`, 401
  - `OCSStrategy` auth header dispatch returns `{"X-api-key": …}` for `api_key`
- `tests/test_tenant_credentials_view.py`
  - POST commcare → 1 membership (regression)
  - POST ocs → N memberships, transactional
  - POST unknown provider → 400
  - POST missing required field → 400
  - PATCH commcare and ocs → re-verifies, updates credential
- `tests/test_api_key_providers_view.py` — `GET` returns registry shape
- `tests/test_ocs_base_loader.py` — header dispatch by credential type

### Frontend

No unit tests (no infrastructure). Manual verification with `playwright-cli`
before claiming done.

### Manual verification checklist

- [ ] Add an OCS connection in local dev → all experiments populate as rows
- [ ] Switch active workspace to an OCS experiment → chat pulls real data
- [ ] Rotate OCS key → connection still works
- [ ] Delete OCS connection → membership and credential gone, no orphans
- [ ] Existing CommCare add/edit/delete still works via the new endpoint shape

## Rollout

Single PR. No feature flag. Cut over backend and frontend together.

## Future work

- CommCare Connect API-key support: drop in a `CommCareConnectStrategy` and
  add to the registry. No view or frontend changes required.
- OnboardingWizard: update once the strategy registry has stabilized.
