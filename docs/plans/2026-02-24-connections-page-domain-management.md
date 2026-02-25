# Connections Page Domain Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the broken Connections page (which only shows OAuth providers and says "No OAuth providers configured" when none exist) with a full domain management UI that lets logged-in users add, edit, and remove CommCare domains connected via API key, alongside existing OAuth provider management.

**Architecture:** The backend API endpoints already exist (`GET/POST /api/auth/tenant-credentials/`, `DELETE /api/auth/tenant-credentials/<id>/`, `PATCH` needs to be added). The frontend `ConnectionsPage` needs to be rewritten to fetch and render both OAuth providers and API-key domains, with inline forms for adding/editing domains. We'll add a `PATCH` endpoint on the backend to update credentials.

**Tech Stack:** Django async views (existing pattern), React 19 + Zustand + Tailwind CSS 4, existing `api` client, existing `Card`/`Button`/`Input`/`Label`/`Dialog` UI components.

---

## Context & Key Files

### Backend
- `apps/users/views.py` — All tenant views live here. Add `PATCH` to `tenant_credential_detail_view`.
- `apps/chat/auth_urls.py` — URL config. `tenant-credentials/<membership_id>/` already handles DELETE; update it to also handle PATCH.
- `apps/users/models.py` — `TenantMembership` (provider, tenant_id, tenant_name) + `TenantCredential` (credential_type, encrypted_credential). Stored as `"username:apikey"`.
- `apps/users/adapters.py` — `encrypt_credential(plain: str) -> str` for Fernet encryption.

### Frontend
- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` — The page to rewrite.
- `frontend/src/components/OnboardingWizard/OnboardingWizard.tsx` — Reference for the API key form pattern (domain, username, api-key fields; POST to `/api/auth/tenant-credentials/`).
- `frontend/src/api/client.ts` — The `api` helper (`.get`, `.post`, `.delete`). Check if it has `.patch`.

### Tests
- `tests/test_tenant_api.py` — Existing test file; add new tests here.
- Backend test pattern: `@pytest.mark.django_db`, `django.test.Client`, `client.force_login(user)`, `client.patch(url, data=..., content_type="application/json")`.

---

## Task 1: Add PATCH support to `api` client (if missing)

**Files:**
- Check: `frontend/src/api/client.ts`

**Step 1: Check if `api.patch` exists**

```bash
grep -n "patch" frontend/src/api/client.ts
```

**Step 2: If missing, add `.patch` method**

Open `frontend/src/api/client.ts` and add a `patch` method mirroring the `post` method but with `method: "PATCH"`. Look at how `post` is implemented and replicate that pattern.

**Step 3: Commit**

```bash
git add frontend/src/api/client.ts
git commit -m "feat: add patch method to api client"
```

---

## Task 2: Add PATCH endpoint for updating an API-key credential

**Files:**
- Modify: `apps/users/views.py` — update `tenant_credential_detail_view`
- Modify: `apps/chat/auth_urls.py` — allow PATCH in `require_http_methods`
- Test: `tests/test_tenant_api.py`

### Step 1: Write the failing test

Add to `tests/test_tenant_api.py`:

```python
@pytest.mark.django_db
class TestTenantCredentialUpdateAPI:
    def test_patch_updates_credential(self, user):
        from apps.users.models import TenantCredential
        from apps.users.adapters import encrypt_credential

        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        TenantCredential.objects.create(
            tenant_membership=tm,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential=encrypt_credential("old@example.com:oldkey"),
        )

        client = Client()
        client.force_login(user)
        response = client.patch(
            f"/api/auth/tenant-credentials/{tm.id}/",
            data={
                "tenant_name": "Dimagi Updated",
                "credential": "new@example.com:newkey",
            },
            content_type="application/json",
        )
        assert response.status_code == 200
        tm.refresh_from_db()
        assert tm.tenant_name == "Dimagi Updated"
        # credential should be updated (just verify it changed, not decrypt)
        tm.credential.refresh_from_db()
        assert tm.credential.encrypted_credential != encrypt_credential("old@example.com:oldkey")

    def test_patch_requires_auth(self):
        client = Client()
        response = client.patch(
            "/api/auth/tenant-credentials/00000000-0000-0000-0000-000000000000/",
            data={"tenant_name": "x"},
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_patch_returns_404_for_wrong_user(self, user, other_user):
        """A user cannot patch another user's credential."""
        tm = TenantMembership.objects.create(
            user=other_user, provider="commcare", tenant_id="dimagi", tenant_name="Dimagi"
        )
        client = Client()
        client.force_login(user)
        response = client.patch(
            f"/api/auth/tenant-credentials/{tm.id}/",
            data={"tenant_name": "hijacked"},
            content_type="application/json",
        )
        assert response.status_code == 404
```

You will need an `other_user` fixture. Check `tests/conftest.py` for existing fixtures — if `other_user` doesn't exist, add it:

```python
# In tests/conftest.py, add:
@pytest.fixture
def other_user(db):
    from apps.users.models import User
    return User.objects.create_user(username="other@example.com", email="other@example.com", password="pass")
```

### Step 2: Run test to verify it fails

```bash
uv run pytest tests/test_tenant_api.py::TestTenantCredentialUpdateAPI -v
```

Expected: FAIL — "Method not allowed" or 405.

### Step 3: Implement PATCH in `apps/users/views.py`

Change the decorator on `tenant_credential_detail_view` to include `"PATCH"` and add the PATCH branch:

```python
@require_http_methods(["DELETE", "PATCH"])
async def tenant_credential_detail_view(request, membership_id):
    """DELETE /api/auth/tenant-credentials/<membership_id>/ — remove a credential
    PATCH  /api/auth/tenant-credentials/<membership_id>/ — update credential"""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    if request.method == "DELETE":
        def _delete():
            try:
                tm = TenantMembership.objects.get(id=membership_id, user=user)
                tm.delete()
                return True
            except TenantMembership.DoesNotExist:
                return False

        deleted = await sync_to_async(_delete)()
        if not deleted:
            return JsonResponse({"error": "Not found"}, status=404)
        return JsonResponse({"status": "deleted"})

    # PATCH
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    tenant_name = body.get("tenant_name", "").strip()
    credential = body.get("credential", "").strip()

    if not tenant_name and not credential:
        return JsonResponse({"error": "At least one of tenant_name or credential is required"}, status=400)

    from apps.users.adapters import encrypt_credential
    from apps.users.models import TenantCredential

    encrypted = None
    if credential:
        try:
            encrypted = await sync_to_async(encrypt_credential)(credential)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=500)

    def _update():
        try:
            tm = TenantMembership.objects.select_related("credential").get(
                id=membership_id, user=user
            )
        except TenantMembership.DoesNotExist:
            return None

        if tenant_name:
            tm.tenant_name = tenant_name
            tm.save(update_fields=["tenant_name"])

        if encrypted and hasattr(tm, "credential"):
            tm.credential.encrypted_credential = encrypted
            tm.credential.save(update_fields=["encrypted_credential"])

        return tm

    tm = await sync_to_async(_update)()
    if tm is None:
        return JsonResponse({"error": "Not found"}, status=404)

    return JsonResponse({"membership_id": str(tm.id), "tenant_name": tm.tenant_name})
```

### Step 4: Run tests to verify they pass

```bash
uv run pytest tests/test_tenant_api.py::TestTenantCredentialUpdateAPI -v
```

Expected: All 3 tests PASS.

### Step 5: Commit

```bash
git add apps/users/views.py tests/test_tenant_api.py tests/conftest.py
git commit -m "feat: add PATCH endpoint for updating tenant credentials"
```

---

## Task 3: Rewrite `ConnectionsPage` — domains section (add & remove)

**Files:**
- Modify: `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx`

The page will have two sections:
1. **CommCare Domains** — lists API-key domains with Edit / Remove buttons, plus an "Add Domain" button that opens an inline form.
2. **OAuth Providers** — the existing OAuth provider list (unchanged).

### Step 1: Read current file

You already have it above, but re-read to confirm before editing:

```bash
cat frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx
```

### Step 2: Write the new `ConnectionsPage.tsx`

Replace the entire file with:

```tsx
import { useState, useEffect, useCallback } from "react"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

interface OAuthProvider {
  id: string
  name: string
  login_url: string
  connected: boolean
}

interface ApiKeyDomain {
  membership_id: string
  provider: string
  tenant_id: string
  tenant_name: string
  credential_type: string
}

type FormMode = "hidden" | "add" | { editing: ApiKeyDomain }

export function ConnectionsPage() {
  const [providers, setProviders] = useState<OAuthProvider[]>([])
  const [domains, setDomains] = useState<ApiKeyDomain[]>([])
  const [loadingProviders, setLoadingProviders] = useState(true)
  const [loadingDomains, setLoadingDomains] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [formMode, setFormMode] = useState<FormMode>("hidden")

  // Form state
  const [formDomain, setFormDomain] = useState("")
  const [formUsername, setFormUsername] = useState("")
  const [formApiKey, setFormApiKey] = useState("")
  const [formLoading, setFormLoading] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const fetchProviders = useCallback(async () => {
    setLoadingProviders(true)
    try {
      const data = await api.get<{ providers: OAuthProvider[] }>("/api/auth/providers/")
      setProviders(data.providers)
    } catch {
      setError("Failed to load OAuth providers.")
    } finally {
      setLoadingProviders(false)
    }
  }, [])

  const fetchDomains = useCallback(async () => {
    setLoadingDomains(true)
    try {
      const data = await api.get<ApiKeyDomain[]>("/api/auth/tenant-credentials/")
      setDomains(data)
    } catch {
      setError("Failed to load connected domains.")
    } finally {
      setLoadingDomains(false)
    }
  }, [])

  useEffect(() => {
    fetchProviders()
    fetchDomains()
  }, [fetchProviders, fetchDomains])

  function openAddForm() {
    setFormDomain("")
    setFormUsername("")
    setFormApiKey("")
    setFormError(null)
    setFormMode("add")
  }

  function openEditForm(domain: ApiKeyDomain) {
    // tenant_id is the domain slug; username is not stored (only encrypted), so leave blank
    setFormDomain(domain.tenant_id)
    setFormUsername("")
    setFormApiKey("")
    setFormError(null)
    setFormMode({ editing: domain })
  }

  function cancelForm() {
    setFormMode("hidden")
    setFormError(null)
  }

  async function handleAddDomain(e: React.FormEvent) {
    e.preventDefault()
    setFormLoading(true)
    setFormError(null)
    try {
      await api.post("/api/auth/tenant-credentials/", {
        provider: "commcare",
        tenant_id: formDomain,
        tenant_name: formDomain,
        credential: `${formUsername}:${formApiKey}`,
      })
      await fetchDomains()
      setFormMode("hidden")
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to add domain.")
    } finally {
      setFormLoading(false)
    }
  }

  async function handleEditDomain(e: React.FormEvent) {
    e.preventDefault()
    if (typeof formMode !== "object") return
    setFormLoading(true)
    setFormError(null)
    const { membership_id } = formMode.editing
    try {
      const body: Record<string, string> = { tenant_name: formDomain }
      if (formUsername && formApiKey) {
        body.credential = `${formUsername}:${formApiKey}`
      }
      await api.patch(`/api/auth/tenant-credentials/${membership_id}/`, body)
      await fetchDomains()
      setFormMode("hidden")
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Failed to update domain.")
    } finally {
      setFormLoading(false)
    }
  }

  async function handleRemoveDomain(membershipId: string) {
    if (!confirm("Remove this domain? This cannot be undone.")) return
    setRemoving(membershipId)
    setError(null)
    try {
      await api.delete(`/api/auth/tenant-credentials/${membershipId}/`)
      await fetchDomains()
    } catch {
      setError("Failed to remove domain.")
    } finally {
      setRemoving(null)
    }
  }

  async function handleDisconnect(providerId: string) {
    setDisconnecting(providerId)
    setError(null)
    try {
      await api.post(`/api/auth/providers/${providerId}/disconnect/`)
      await fetchProviders()
    } catch {
      setError("Failed to disconnect provider.")
    } finally {
      setDisconnecting(null)
    }
  }

  const isEditing = typeof formMode === "object"

  return (
    <div className="mx-auto max-w-2xl space-y-8 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Connected Accounts</h1>
        <p className="text-sm text-muted-foreground">
          Manage your external account connections.
        </p>
      </div>

      {error && (
        <p className="text-sm text-destructive" data-testid="connections-error">
          {error}
        </p>
      )}

      {/* CommCare Domains section */}
      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">CommCare Domains (API Key)</h2>
          {formMode === "hidden" && (
            <Button
              size="sm"
              variant="outline"
              onClick={openAddForm}
              data-testid="add-domain-button"
            >
              Add Domain
            </Button>
          )}
        </div>

        {loadingDomains ? (
          <p className="text-sm text-muted-foreground">Loading domains...</p>
        ) : domains.length === 0 && formMode === "hidden" ? (
          <p className="text-sm text-muted-foreground">No API key domains connected.</p>
        ) : null}

        {domains.map((domain) => (
          <Card key={domain.membership_id}>
            <CardContent className="flex items-center justify-between p-4">
              <div>
                <p className="font-medium" data-testid={`domain-name-${domain.tenant_id}`}>
                  {domain.tenant_name || domain.tenant_id}
                </p>
                <p className="text-sm text-muted-foreground">{domain.tenant_id}</p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => openEditForm(domain)}
                  data-testid={`edit-domain-${domain.tenant_id}`}
                >
                  Edit
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleRemoveDomain(domain.membership_id)}
                  disabled={removing === domain.membership_id}
                  data-testid={`remove-domain-${domain.tenant_id}`}
                >
                  {removing === domain.membership_id ? "Removing..." : "Remove"}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}

        {/* Add / Edit form */}
        {formMode !== "hidden" && (
          <Card data-testid="domain-form">
            <CardContent className="p-4">
              <form
                onSubmit={isEditing ? handleEditDomain : handleAddDomain}
                className="space-y-4"
              >
                <p className="font-medium">{isEditing ? "Edit Domain" : "Add Domain"}</p>
                <div className="space-y-2">
                  <Label htmlFor="form-domain">CommCare Domain</Label>
                  <Input
                    id="form-domain"
                    data-testid="domain-form-domain"
                    required
                    placeholder="my-project"
                    value={formDomain}
                    onChange={(e) => setFormDomain(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="form-username">
                    CommCare Username{isEditing ? " (leave blank to keep existing)" : ""}
                  </Label>
                  <Input
                    id="form-username"
                    data-testid="domain-form-username"
                    type="email"
                    required={!isEditing}
                    placeholder="you@example.com"
                    value={formUsername}
                    onChange={(e) => setFormUsername(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="form-api-key">
                    API Key{isEditing ? " (leave blank to keep existing)" : ""}
                  </Label>
                  <Input
                    id="form-api-key"
                    data-testid="domain-form-api-key"
                    type="password"
                    required={!isEditing}
                    value={formApiKey}
                    onChange={(e) => setFormApiKey(e.target.value)}
                  />
                </div>
                {formError && (
                  <p className="text-sm text-destructive" data-testid="domain-form-error">
                    {formError}
                  </p>
                )}
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="flex-1"
                    onClick={cancelForm}
                  >
                    Cancel
                  </Button>
                  <Button type="submit" className="flex-1" disabled={formLoading}>
                    {formLoading ? "Saving..." : isEditing ? "Save Changes" : "Add Domain"}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        )}
      </section>

      {/* OAuth Providers section */}
      <section className="space-y-4">
        <h2 className="text-lg font-medium">OAuth Providers</h2>
        {loadingProviders ? (
          <p className="text-sm text-muted-foreground">Loading providers...</p>
        ) : providers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No OAuth providers configured.</p>
        ) : (
          providers.map((provider) => (
            <Card key={provider.id}>
              <CardContent className="flex items-center justify-between p-4">
                <div>
                  <p className="font-medium">{provider.name}</p>
                  <p className="text-sm text-muted-foreground">
                    {provider.connected ? "Connected" : "Not connected"}
                  </p>
                </div>
                {provider.connected ? (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleDisconnect(provider.id)}
                    disabled={disconnecting === provider.id}
                    data-testid={`disconnect-${provider.id}`}
                  >
                    {disconnecting === provider.id ? "Disconnecting..." : "Disconnect"}
                  </Button>
                ) : (
                  <Button variant="outline" size="sm" asChild data-testid={`connect-${provider.id}`}>
                    <a href={`${provider.login_url}?process=connect&next=/settings/connections`}>
                      Connect
                    </a>
                  </Button>
                )}
              </CardContent>
            </Card>
          ))
        )}
      </section>
    </div>
  )
}
```

### Step 3: Verify `api.delete` exists in the client

```bash
grep -n "delete\|patch" frontend/src/api/client.ts
```

If `api.delete` doesn't exist, add it (same pattern as `api.post` but `method: "DELETE"` and no body).

### Step 4: Build to check for TypeScript errors

```bash
cd frontend && bun run build 2>&1 | head -50
```

Fix any TypeScript errors before continuing.

### Step 5: Commit

```bash
git add frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx frontend/src/api/client.ts
git commit -m "feat: rewrite ConnectionsPage with CommCare domain management (add/edit/remove)"
```

---

## Task 4: Validate PATCH endpoint handles partial updates correctly

The edit form allows the user to update the domain slug/name without re-entering credentials. But the current `tenant_credential_list_view` POST always uses `tenant_id` as the `tenant_name` — the PATCH should allow `tenant_name` to differ from `tenant_id`.

**This is already handled by the Task 2 implementation.** Double-check:

```bash
uv run pytest tests/test_tenant_api.py -v
```

All tests should pass. If any fail, fix them before continuing.

---

## Task 5: Manual smoke test

Start the dev servers and verify the following scenarios work:

**Setup:**
```bash
uv run honcho -f Procfile.dev start
```

Navigate to `http://localhost:5173/settings/connections`.

**Scenario A: Add a new domain**
1. Page shows "CommCare Domains (API Key)" section with an "Add Domain" button.
2. Click "Add Domain" — form appears inline.
3. Fill in domain, username, api-key. Click "Add Domain".
4. Form disappears; new domain card appears in the list.

**Scenario B: Edit an existing domain**
1. Click "Edit" on a domain card.
2. Form appears pre-filled with the domain name (username/key fields blank).
3. Change the domain name field; leave username/key blank.
4. Click "Save Changes" — domain card updates with new name (PATCH with `tenant_name` only).

**Scenario C: Remove a domain**
1. Click "Remove" on a domain.
2. Confirmation dialog appears.
3. Confirm — domain card disappears.

**Scenario D: OAuth section still works**
- If CommCare OAuth is configured, the OAuth section shows providers as before.
- If not configured, it shows "No OAuth providers configured." rather than making the whole page blank.

---

## Task 6: Run full test suite

```bash
uv run pytest tests/test_tenant_api.py tests/test_tenant_models.py tests/test_auth.py -v
```

All tests must pass before committing.

If any pre-existing tests break (they shouldn't), investigate and fix.

---

## Notes

### API client `patch` / `delete` methods
If `api.delete` or `api.patch` don't exist in `frontend/src/api/client.ts`, add them following the same pattern as `api.post`. The method signature should be:

```ts
async patch<T = unknown>(path: string, body?: unknown): Promise<T>
async delete<T = unknown>(path: string): Promise<T>
```

### Edit form: credentials are write-only
The API does not return decrypted credentials (by design). The edit form pre-fills the domain name but leaves username/api-key blank. The backend PATCH only updates the credential if both username and api-key are provided (enforced client-side by checking `formUsername && formApiKey`). This is the correct UX pattern for sensitive credentials.

### `tenant_name` vs `tenant_id`
`tenant_id` is the CommCare domain slug (e.g., `"my-project"`) and cannot be changed after creation (it's part of the unique constraint). `tenant_name` is the display name and starts equal to `tenant_id` but can be updated. The edit form pre-fills the `formDomain` field with `tenant_id` and sends it as `tenant_name` in the PATCH body.

### `confirm()` for delete
The "Remove" action uses `window.confirm()` for simplicity. This is acceptable for now. Do not add a modal/dialog unless explicitly requested — YAGNI.
