# Canonical Tenant Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the raw `tenant_id` string authorization boundary with a canonical `Tenant` model so that cross-tenant access via guessed/spoofed identifiers is structurally impossible.

**Architecture:** Add `Tenant(provider, external_id)` to `apps/users/models.py` as the verified identity record. `TenantMembership` gets a FK to `Tenant` (losing its own `provider/tenant_id/tenant_name` fields). `TenantWorkspace` gets a OneToOneField to `Tenant` (losing `tenant_id`/`tenant_name` CharFields). All auth checks switch from string comparison to FK comparison. The credential endpoint verifies the API key against CommCare before creating a `Tenant`.

**Tech Stack:** Django 5, Django migrations, pytest, `requests` (for CommCare verification), `unittest.mock` (for tests)

---

### Task 1: Add `Tenant` model and migration

**Files:**
- Modify: `apps/users/models.py`
- Create: `apps/users/migrations/0005_tenant.py`

**Step 1: Add `Tenant` class to models.py**

In `apps/users/models.py`, after the `TenantMembership.PROVIDER_CHOICES` (which you'll extract to module level), add before `TenantMembership`:

```python
PROVIDER_CHOICES = [
    ("commcare", "CommCare HQ"),
    ("commcare_connect", "CommCare Connect"),
]


class Tenant(models.Model):
    """Canonical tenant identity record, created only after provider verification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    external_id = models.CharField(
        max_length=255,
        help_text="Provider-assigned identifier (CommCare domain name or Connect org ID).",
    )
    canonical_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["provider", "external_id"]
        ordering = ["canonical_name"]

    def __str__(self):
        return f"{self.provider}:{self.external_id} ({self.canonical_name})"
```

Also update `TenantMembership.PROVIDER_CHOICES` to reference the module-level constant:
```python
class TenantMembership(models.Model):
    PROVIDER_CHOICES = PROVIDER_CHOICES  # reference module-level constant
    ...
```

**Step 2: Write the failing test**

In `tests/test_tenant_models.py`, add:

```python
class TestTenant:
    def test_create_tenant(self, db):
        from apps.users.models import Tenant
        t = Tenant.objects.create(
            provider="commcare",
            external_id="dimagi",
            canonical_name="Dimagi",
        )
        assert t.provider == "commcare"
        assert t.external_id == "dimagi"
        assert str(t) == "commcare:dimagi (Dimagi)"

    def test_unique_constraint(self, db):
        from apps.users.models import Tenant
        Tenant.objects.create(provider="commcare", external_id="dimagi", canonical_name="Dimagi")
        with pytest.raises(Exception):  # noqa: B017
            Tenant.objects.create(provider="commcare", external_id="dimagi", canonical_name="Dimagi2")
```

**Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_tenant_models.py::TestTenant -v
```
Expected: FAIL — `Tenant` not yet in DB (no migration run yet)

**Step 4: Generate and run migration**

```bash
uv run python manage.py makemigrations users --name tenant
uv run python manage.py migrate
```

**Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_tenant_models.py -v
```
Expected: All PASS

**Step 6: Commit**

```bash
git add apps/users/models.py apps/users/migrations/0005_tenant.py tests/test_tenant_models.py
git commit -m "feat: add canonical Tenant model (#52)"
```

---

### Task 2: Migrate `TenantMembership` to FK on `Tenant`

**Files:**
- Modify: `apps/users/models.py`
- Create: `apps/users/migrations/0006_tenantmembership_use_tenant_fk.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_tenant_models.py`

**Context:** `TenantMembership` currently has `provider`, `tenant_id`, `tenant_name` fields and unique constraint `["user", "provider", "tenant_id"]`. We replace these with a single FK to `Tenant` and unique constraint `["user", "tenant"]`.

**Step 1: Update `TenantMembership` in models.py**

Replace the existing `TenantMembership` class with:

```python
class TenantMembership(models.Model):
    """Links a user to a verified Tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    last_selected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "tenant"]
        ordering = ["-last_selected_at", "tenant__canonical_name"]

    def __str__(self):
        return f"{self.user.email} - {self.tenant}"

    # Convenience properties to avoid updating all callsites at once
    @property
    def provider(self):
        return self.tenant.provider

    @property
    def tenant_id(self):
        return self.tenant.external_id

    @property
    def tenant_name(self):
        return self.tenant.canonical_name
```

> Note: The three `@property` shims keep existing call sites (`tm.tenant_id`, `tm.provider`, `tm.tenant_name`) working during the migration so we don't have to update every caller at once. We'll remove them at the end.

**Step 2: Write the failing test for the new model shape**

In `tests/test_tenant_models.py`, add:

```python
class TestTenantMembership:
    def test_create_membership_with_tenant(self, db, user):
        from apps.users.models import Tenant, TenantMembership
        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        tm = TenantMembership.objects.create(user=user, tenant=tenant)
        assert tm.tenant_id == "dimagi"   # via property
        assert tm.provider == "commcare"  # via property
        assert tm.tenant_name == "Dimagi" # via property
        assert str(tm) == f"{user.email} - commcare:dimagi (Dimagi)"

    def test_unique_constraint(self, db, user):
        from apps.users.models import Tenant, TenantMembership
        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        TenantMembership.objects.create(user=user, tenant=tenant)
        with pytest.raises(Exception):  # noqa: B017
            TenantMembership.objects.create(user=user, tenant=tenant)
```

**Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_tenant_models.py::TestTenantMembership -v
```
Expected: FAIL — old migration still in place

**Step 4: Generate migration**

```bash
uv run python manage.py makemigrations users --name tenantmembership_use_tenant_fk
```

Inspect the generated migration. It should drop `provider`, `tenant_id`, `tenant_name` from `TenantMembership` and add `tenant` FK. If Django generated a data migration placeholder, fill it in or remove it (no data to migrate).

**Step 5: Run migration**

```bash
uv run python manage.py migrate
```

**Step 6: Update conftest.py fixtures**

In `tests/conftest.py`, update the `tenant_membership` fixture:

```python
@pytest.fixture
def tenant(db):
    from apps.users.models import Tenant
    return Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )


@pytest.fixture
def tenant_membership(db, user, tenant):
    from apps.users.models import TenantMembership
    return TenantMembership.objects.create(user=user, tenant=tenant)


@pytest.fixture
def workspace(db, tenant):
    from apps.projects.models import TenantWorkspace
    # Temporarily still uses tenant_id CharField — will be updated in Task 3
    return TenantWorkspace.objects.create(
        tenant_id=tenant.external_id,
        tenant_name=tenant.canonical_name,
    )
```

**Step 7: Run all tests**

```bash
uv run pytest tests/test_tenant_models.py tests/test_tenant_api.py tests/test_tenant_resolution.py -v
```
Expected: All PASS (property shims keep callers working)

**Step 8: Commit**

```bash
git add apps/users/models.py apps/users/migrations/0006_tenantmembership_use_tenant_fk.py tests/conftest.py tests/test_tenant_models.py
git commit -m "feat: migrate TenantMembership to FK on Tenant (#52)"
```

---

### Task 3: Migrate `TenantWorkspace` to OneToOneField on `Tenant`

**Files:**
- Modify: `apps/projects/models.py`
- Create: `apps/projects/migrations/0016_tenantworkspace_use_tenant_fk.py`
- Modify: `tests/conftest.py`

**Step 1: Update `TenantWorkspace` in `apps/projects/models.py`**

Replace:
```python
tenant_id = models.CharField(
    max_length=255,
    unique=True,
    help_text="Domain name (CommCare) or organization ID. One workspace per tenant.",
)
tenant_name = models.CharField(max_length=255)
```

With:
```python
tenant = models.OneToOneField(
    "users.Tenant",
    on_delete=models.CASCADE,
    related_name="workspace",
)
```

Update `__str__` and `Meta.ordering`:
```python
class Meta:
    ordering = ["tenant__canonical_name"]

def __str__(self):
    return f"{self.tenant.canonical_name} ({self.tenant.external_id})"
```

Add convenience properties to keep existing callsites working:
```python
@property
def tenant_id(self):
    return self.tenant.external_id

@property
def tenant_name(self):
    return self.tenant.canonical_name
```

**Step 2: Write the failing test**

In a new `tests/test_models.py` section (or append to `tests/test_models.py`):

```python
@pytest.mark.django_db
class TestTenantWorkspace:
    def test_create_workspace_with_tenant(self):
        from apps.projects.models import TenantWorkspace
        from apps.users.models import Tenant
        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        ws = TenantWorkspace.objects.create(tenant=tenant)
        assert ws.tenant_id == "dimagi"       # via property
        assert ws.tenant_name == "Dimagi"     # via property
        assert str(ws) == "Dimagi (dimagi)"

    def test_one_workspace_per_tenant(self):
        from apps.projects.models import TenantWorkspace
        from apps.users.models import Tenant
        tenant = Tenant.objects.create(
            provider="commcare", external_id="dimagi", canonical_name="Dimagi"
        )
        TenantWorkspace.objects.create(tenant=tenant)
        with pytest.raises(Exception):  # noqa: B017
            TenantWorkspace.objects.create(tenant=tenant)
```

**Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_models.py::TestTenantWorkspace -v
```

**Step 4: Generate and run migration**

```bash
uv run python manage.py makemigrations projects --name tenantworkspace_use_tenant_fk
uv run python manage.py migrate
```

**Step 5: Update conftest.py `workspace` fixture**

```python
@pytest.fixture
def workspace(db, tenant):
    from apps.projects.models import TenantWorkspace
    return TenantWorkspace.objects.create(tenant=tenant)
```

**Step 6: Run tests**

```bash
uv run pytest tests/test_models.py tests/test_tenant_api.py -v
```
Expected: All PASS

**Step 7: Commit**

```bash
git add apps/projects/models.py apps/projects/migrations/0016_tenantworkspace_use_tenant_fk.py tests/conftest.py tests/test_models.py
git commit -m "feat: migrate TenantWorkspace to FK on Tenant (#52)"
```

---

### Task 4: Update `tenant_resolution.py` to upsert `Tenant` first

**Files:**
- Modify: `apps/users/services/tenant_resolution.py`
- Modify: `tests/test_tenant_resolution.py`
- Modify: `tests/test_connect_tenant_resolution.py`

**Context:** `resolve_commcare_domains` and `resolve_connect_opportunities` currently do `TenantMembership.objects.update_or_create(user=user, provider=..., tenant_id=..., defaults={"tenant_name": ...})`. They need to upsert `Tenant` first, then upsert `TenantMembership(user, tenant)`.

**Step 1: Update `resolve_commcare_domains`**

```python
def resolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and upsert Tenant + TenantMembership records."""
    from apps.users.models import Tenant

    domains = _fetch_all_domains(access_token)
    memberships = []

    for domain in domains:
        tenant, _ = Tenant.objects.update_or_create(
            provider="commcare",
            external_id=domain["domain_name"],
            defaults={"canonical_name": domain["project_name"]},
        )
        tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
        TenantCredential.objects.get_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)

    logger.info("Resolved %d CommCare domains for user %s", len(memberships), user.email)
    return memberships
```

**Step 2: Update `resolve_connect_opportunities`**

Apply the same pattern — upsert `Tenant(provider="commcare_connect", external_id=str(opp["id"]))` first, then `TenantMembership.objects.get_or_create(user=user, tenant=tenant)`.

**Step 3: Run the resolution tests**

```bash
uv run pytest tests/test_tenant_resolution.py tests/test_connect_tenant_resolution.py -v
```

The existing assertions like `memberships[0].tenant_id == "dimagi"` still work via the property shim. Update any assertions that construct `TenantMembership` directly (they'll now need a `Tenant` to exist first).

Expected: All PASS

**Step 4: Commit**

```bash
git add apps/users/services/tenant_resolution.py tests/test_tenant_resolution.py tests/test_connect_tenant_resolution.py
git commit -m "refactor: tenant_resolution upserts Tenant before TenantMembership (#52)"
```

---

### Task 5: Add CommCare API-key verification service

**Files:**
- Create: `apps/users/services/tenant_verification.py`
- Create: `tests/test_tenant_verification.py`

**Context:** The credential endpoint must verify a CommCare API key before creating a `Tenant`. CommCare's web-user API (`GET /a/{domain}/api/v0.5/web-user/{username}/`) returns 200 if credentials are valid and the user is a member of that domain.

**Step 1: Write the failing test**

```python
# tests/test_tenant_verification.py
from unittest.mock import MagicMock, patch

import pytest


class TestVerifyCommcareCredential:
    def test_valid_credential_returns_domain_info(self):
        from apps.users.services.tenant_verification import verify_commcare_credential

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"username": "user@dimagi.org", "domain": "dimagi"}

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            result = verify_commcare_credential(
                domain="dimagi", username="user@dimagi.org", api_key="secret"
            )

        assert result["domain"] == "dimagi"

    def test_invalid_credential_raises(self):
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError):
                verify_commcare_credential(
                    domain="dimagi", username="user@dimagi.org", api_key="wrong"
                )

    def test_wrong_domain_raises(self):
        """User exists but doesn't belong to the claimed domain."""
        from apps.users.services.tenant_verification import (
            CommCareVerificationError,
            verify_commcare_credential,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("apps.users.services.tenant_verification.requests.get", return_value=mock_resp):
            with pytest.raises(CommCareVerificationError):
                verify_commcare_credential(
                    domain="other-domain", username="user@dimagi.org", api_key="secret"
                )
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_tenant_verification.py -v
```
Expected: FAIL — module doesn't exist yet

**Step 3: Create the verification service**

```python
# apps/users/services/tenant_verification.py
"""Verify provider credentials before creating Tenant records."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareVerificationError(Exception):
    """Raised when CommCare credential verification fails."""


def verify_commcare_credential(domain: str, username: str, api_key: str) -> dict:
    """Verify a CommCare API key against the CommCare web-user API.

    Calls GET /a/{domain}/api/v0.5/web-user/{username}/ with the supplied
    API key. Returns the user info dict on success.

    Raises CommCareVerificationError if the credential is invalid, the user
    doesn't exist, or the user is not a member of the domain.
    """
    url = f"{COMMCARE_API_BASE}/a/{domain}/api/v0.5/web-user/{username}/"
    resp = requests.get(
        url,
        headers={"Authorization": f"ApiKey {username}:{api_key}"},
        timeout=15,
    )
    if resp.status_code in (401, 403):
        raise CommCareVerificationError(
            f"CommCare rejected the API key for domain '{domain}' (HTTP {resp.status_code})"
        )
    if resp.status_code == 404:
        raise CommCareVerificationError(
            f"User '{username}' not found in domain '{domain}'"
        )
    if not resp.ok:
        raise CommCareVerificationError(
            f"CommCare API returned unexpected status {resp.status_code}"
        )
    return resp.json()
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_tenant_verification.py -v
```
Expected: All PASS

**Step 5: Commit**

```bash
git add apps/users/services/tenant_verification.py tests/test_tenant_verification.py
git commit -m "feat: CommCare API-key verification service (#52)"
```

---

### Task 6: Update credential endpoint to verify before creating Tenant

**Files:**
- Modify: `apps/users/views.py`
- Modify: `tests/test_tenant_api.py`

**Context:** The `tenant_credential_list_view` POST handler currently creates `TenantMembership` from raw user input without verification. We change it to:
1. Parse `credential` as `"username:apikey"`
2. Call `verify_commcare_credential(domain, username, api_key)`
3. On success: upsert `Tenant`, then `TenantMembership`, then `TenantCredential`
4. On failure: return HTTP 400

**Step 1: Write security tests first**

Add to `tests/test_tenant_api.py`:

```python
from unittest.mock import patch

from apps.users.services.tenant_verification import CommCareVerificationError


@pytest.mark.django_db
class TestTenantCredentialCreateAPI:
    def test_create_with_valid_credential(self, user):
        """Valid credential creates Tenant + TenantMembership."""
        from apps.users.models import Tenant, TenantMembership

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.verify_commcare_credential",
            return_value={"domain": "dimagi", "username": "user@dimagi.org"},
        ):
            response = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "tenant_id": "dimagi",
                    "tenant_name": "Dimagi",
                    "credential": "user@dimagi.org:apikey123",
                },
                content_type="application/json",
            )

        assert response.status_code == 201
        assert Tenant.objects.filter(provider="commcare", external_id="dimagi").exists()
        assert TenantMembership.objects.filter(user=user, tenant__external_id="dimagi").exists()

    def test_create_with_invalid_credential_is_rejected(self, user):
        """Invalid credential must not create any records."""
        from apps.users.models import Tenant, TenantMembership

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.verify_commcare_credential",
            side_effect=CommCareVerificationError("Invalid"),
        ):
            response = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "tenant_id": "victim-domain",
                    "tenant_name": "Victim",
                    "credential": "attacker@evil.com:badkey",
                },
                content_type="application/json",
            )

        assert response.status_code == 400
        assert not Tenant.objects.filter(external_id="victim-domain").exists()
        assert not TenantMembership.objects.filter(user=user).exists()

    def test_cross_tenant_access_blocked_by_structure(self, user, other_user):
        """A user who guesses another tenant's external_id cannot gain access
        because they cannot create a TenantMembership without a verified Tenant."""
        from apps.users.models import Tenant, TenantMembership

        # Simulate victim tenant exists (created by other_user via OAuth)
        victim_tenant = Tenant.objects.create(
            provider="commcare", external_id="victim-domain", canonical_name="Victim"
        )
        TenantMembership.objects.create(user=other_user, tenant=victim_tenant)

        # Attacker tries to POST with the victim's tenant_id but bad creds
        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.verify_commcare_credential",
            side_effect=CommCareVerificationError("Invalid"),
        ):
            response = client.post(
                "/api/auth/tenant-credentials/",
                data={
                    "provider": "commcare",
                    "tenant_id": "victim-domain",
                    "tenant_name": "Victim",
                    "credential": "attacker@evil.com:wrongkey",
                },
                content_type="application/json",
            )

        assert response.status_code == 400
        assert not TenantMembership.objects.filter(user=user).exists()
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_tenant_api.py::TestTenantCredentialCreateAPI -v
```
Expected: FAIL

**Step 3: Update the POST handler in `apps/users/views.py`**

Replace the POST section of `tenant_credential_list_view` (lines ~161-206):

```python
    # POST — create API-key-backed membership with provider verification
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    provider = body.get("provider", "").strip()
    tenant_id = body.get("tenant_id", "").strip()
    tenant_name = body.get("tenant_name", "").strip()
    credential = body.get("credential", "").strip()

    if not all([provider, tenant_id, tenant_name, credential]):
        return JsonResponse(
            {"error": "provider, tenant_id, tenant_name, and credential are required"},
            status=400,
        )

    if provider != "commcare":
        return JsonResponse(
            {"error": f"API-key credentials are not supported for provider '{provider}'"},
            status=400,
        )

    # credential must be "username:apikey"
    if ":" not in credential:
        return JsonResponse(
            {"error": "credential must be in the format 'username:apikey'"},
            status=400,
        )
    cc_username, cc_api_key = credential.split(":", 1)

    from apps.users.services.tenant_verification import (
        CommCareVerificationError,
        verify_commcare_credential,
    )

    try:
        verified = await sync_to_async(verify_commcare_credential)(
            domain=tenant_id, username=cc_username, api_key=cc_api_key
        )
    except CommCareVerificationError as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Use verified name if available, fall back to user-supplied name
    verified_name = verified.get("domain") or tenant_name

    from django.db import transaction

    from apps.users.adapters import encrypt_credential
    from apps.users.models import Tenant, TenantCredential

    try:
        encrypted = await sync_to_async(encrypt_credential)(credential)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=500)

    def _create():
        with transaction.atomic():
            tenant, _ = Tenant.objects.update_or_create(
                provider=provider,
                external_id=tenant_id,
                defaults={"canonical_name": verified_name},
            )
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
            TenantCredential.objects.update_or_create(
                tenant_membership=tm,
                defaults={
                    "credential_type": TenantCredential.API_KEY,
                    "encrypted_credential": encrypted,
                },
            )
            return tm

    tm = await sync_to_async(_create)()
    return JsonResponse({"membership_id": str(tm.id)}, status=201)
```

Also add the import at the top of `tenant_credential_list_view` (inside the function, since it's already imported locally):

At the top of `views.py` file-level imports, it already imports `TenantMembership`; add a `verify_commcare_credential` import inside the POST branch as shown above.

**Step 4: Run tests**

```bash
uv run pytest tests/test_tenant_api.py -v
```
Expected: All PASS

**Step 5: Commit**

```bash
git add apps/users/views.py tests/test_tenant_api.py
git commit -m "fix: verify CommCare credential before creating Tenant (closes #52)"
```

---

### Task 7: Update authorization checks from `tenant_id=` string to `tenant=` FK

**Files:**
- Modify: `apps/artifacts/views.py`
- Modify: `apps/recipes/api/views.py`
- Modify: `apps/projects/api/views.py`
- Modify: `apps/knowledge/api/views.py`

**Context:** All these files do `TenantMembership.objects.filter(user=..., tenant_id=workspace.tenant_id)`. Since `workspace.tenant_id` is now a property returning `workspace.tenant.external_id`, the query would still work — but it's a string comparison. We want `filter(user=..., tenant=workspace.tenant)` which is an FK join and is structurally unforgeable.

**Step 1: Update `apps/artifacts/views.py`**

There are 5 occurrences. In each:
```python
# Before
TenantMembership.objects.filter(user=request.user, tenant_id=artifact.workspace.tenant_id)
# After
TenantMembership.objects.filter(user=request.user, tenant=artifact.workspace.tenant)
```

For the async variant (line ~762):
```python
# Before
await TenantMembership.objects.filter(user=user, tenant_id=artifact.workspace.tenant_id).aexists()
# After
await TenantMembership.objects.filter(user=user, tenant=artifact.workspace.tenant).aexists()
```

For `ArtifactListView` (line ~953), also update:
```python
# Before
workspace, _ = TenantWorkspace.objects.get_or_create(
    tenant_id=membership.tenant_id,
    defaults={"tenant_name": membership.tenant_name},
)
# After
workspace, _ = TenantWorkspace.objects.get_or_create(
    tenant=membership.tenant,
)
```

For `ArtifactQueryDataView.get`, also update `load_tenant_context` call (line ~775) — this still needs `tenant.external_id` as a string:
```python
ctx = await load_tenant_context(artifact.workspace.tenant.external_id)
```

**Step 2: Update `apps/recipes/api/views.py`**

```python
# Before
workspace, _ = TenantWorkspace.objects.get_or_create(
    tenant_id=membership.tenant_id,
    defaults={"tenant_name": membership.tenant_name},
)
# After
workspace, _ = TenantWorkspace.objects.get_or_create(
    tenant=membership.tenant,
)
```

**Step 3: Update `apps/projects/api/views.py`**

Two places:
1. `_resolve_workspace` (line ~37):
```python
workspace, _ = TenantWorkspace.objects.get_or_create(
    tenant=membership.tenant,
)
```

2. `_resolve_tenant_schema` (line ~52):
```python
# Before
TenantSchema.objects.filter(
    tenant_membership__tenant_id=membership.tenant_id,
    ...
)
# After
TenantSchema.objects.filter(
    tenant_membership__tenant=membership.tenant,
    ...
)
```

Also line ~270 (`_get_tenant_metadata`):
```python
# Before
def _get_tenant_metadata(tenant_id: str):
    return TenantMetadata.objects.filter(tenant_membership__tenant_id=tenant_id).first()
# After
def _get_tenant_metadata(tenant):
    return TenantMetadata.objects.filter(tenant_membership__tenant=tenant).first()
```

Update all callers of `_get_tenant_metadata` in the same file to pass `tenant_schema.tenant_membership.tenant`.

**Step 4: Update `apps/knowledge/api/views.py`**

Line ~57: same `_resolve_workspace` helper pattern:
```python
workspace, _ = TenantWorkspace.objects.get_or_create(
    tenant=membership.tenant,
)
```

Line ~270: file download uses `workspace.tenant_id` in filename — update to `workspace.tenant.external_id`:
```python
safe_name = workspace.tenant.external_id.replace("/", "_")
```

**Step 5: Run the full test suite**

```bash
uv run pytest tests/ -v --ignore=tests/smoke -x
```
Expected: All PASS

**Step 6: Commit**

```bash
git add apps/artifacts/views.py apps/recipes/api/views.py apps/projects/api/views.py apps/knowledge/api/views.py
git commit -m "refactor: switch authz checks to FK comparison (tenant= not tenant_id=) (#52)"
```

---

### Task 8: Update agent graph and state to use `tenant.external_id`

**Files:**
- Modify: `apps/agents/graph/base.py`
- Modify: `apps/agents/graph/state.py`
- Modify: `apps/chat/views.py`

**Context:** `AgentState` has a `tenant_id: str` field. The agent graph builds `TenantWorkspace` with `get_or_create(tenant_id=...)`. These need to shift to use `tenant.external_id` for the string value and `tenant=` for the FK lookup.

**Step 1: Update `apps/agents/graph/base.py`**

Line ~298:
```python
# Before
workspace, _ = await TenantWorkspace.objects.aget_or_create(
    tenant_id=tenant_membership.tenant_id,
    defaults={"tenant_name": tenant_membership.tenant_name},
)
# After
workspace, _ = await TenantWorkspace.objects.aget_or_create(
    tenant=tenant_membership.tenant,
)
```

Line ~137 (TenantSchema lookup):
```python
# Before
ts = await TenantSchema.objects.filter(
    tenant_membership__tenant_id=tenant_membership.tenant_id,
    ...
).afirst()
# After
ts = await TenantSchema.objects.filter(
    tenant_membership__tenant=tenant_membership.tenant,
    ...
).afirst()
```

Line ~176 (load_tenant_context — still needs string):
```python
ctx = await load_tenant_context(tenant_membership.tenant.external_id)
```

**Step 2: `apps/chat/views.py` — agent state construction**

Line ~620: `tenant_id` in the state dict is the external_id string (used by MCP tools). This is correct — keep using `tenant_membership.tenant_id` (the property still returns `external_id`). No change needed here.

**Step 3: Run agent-related tests**

```bash
uv run pytest tests/test_agent_graph.py tests/agents/ -v
```
Expected: All PASS

**Step 4: Commit**

```bash
git add apps/agents/graph/base.py apps/chat/views.py
git commit -m "refactor: update agent graph to use tenant FK for workspace lookup (#52)"
```

---

### Task 9: Update `projects/admin.py` and `projects/services/schema_manager.py`

**Files:**
- Modify: `apps/projects/admin.py`
- Modify: `apps/projects/services/schema_manager.py`

**Step 1: Update `apps/projects/admin.py`**

```python
@admin.register(TenantWorkspace)
class TenantWorkspaceAdmin(admin.ModelAdmin):
    list_display = ["tenant_name", "tenant_id", "created_at", "updated_at"]
    search_fields = ["tenant__canonical_name", "tenant__external_id"]
    readonly_fields = ["id", "created_at", "updated_at"]
```

The `list_display` uses the property shims so it still works. Or update directly:
```python
    list_display = ["get_tenant_name", "get_tenant_id", "created_at", "updated_at"]
```
...but property shims make this unnecessary. Leave as-is for now.

**Step 2: Update `apps/projects/services/schema_manager.py`**

Line ~40:
```python
# Before
schema_name = self._sanitize_schema_name(tenant_membership.tenant_id)
# After (still uses external_id string — correct)
schema_name = self._sanitize_schema_name(tenant_membership.tenant.external_id)
```

Line ~111's `_sanitize_schema_name(tenant_id: str)` parameter name is fine — it still receives a string.

**Step 3: Run schema manager tests**

```bash
uv run pytest tests/test_schema_manager.py -v
```
Expected: All PASS

**Step 4: Commit**

```bash
git add apps/projects/admin.py apps/projects/services/schema_manager.py
git commit -m "refactor: update admin and schema_manager to use Tenant FK (#52)"
```

---

### Task 10: Remove property shims and run full suite

**Files:**
- Modify: `apps/users/models.py`
- Modify: `apps/projects/models.py`
- Modify: any remaining direct `.tenant_id` / `.tenant_name` accesses on memberships/workspaces

**Context:** The `@property` shims on `TenantMembership` and `TenantWorkspace` were scaffolding. Now that all callers have been updated, remove them and fix any remaining breakage.

**Step 1: Run the full suite to find remaining shim usages**

```bash
uv run pytest tests/ --ignore=tests/smoke -x 2>&1 | head -80
```

**Step 2: Remove shims from `TenantMembership`**

Delete:
```python
@property
def provider(self): ...
@property
def tenant_id(self): ...
@property
def tenant_name(self): ...
```

**Step 3: Remove shims from `TenantWorkspace`**

Delete:
```python
@property
def tenant_id(self): ...
@property
def tenant_name(self): ...
```

**Step 4: Run full suite**

```bash
uv run pytest tests/ --ignore=tests/smoke -v
```

Fix any remaining failures by updating callers to use `.tenant.external_id`, `.tenant.canonical_name`, `.tenant.provider` directly.

**Step 5: Commit**

```bash
git add -u
git commit -m "refactor: remove Tenant property shims, all callers use FK directly (#52)"
```

---

### Task 11: Update existing tests that construct `TenantMembership` directly

**Files:**
- Modify: `tests/test_tenant_models.py` (old `TestTenantMembership` class from before the migration)
- Modify: `apps/artifacts/tests/test_share_api.py`
- Modify: `apps/artifacts/tests/test_artifact_query_data.py`
- Modify: any other test file that calls `TenantMembership.objects.create(provider=..., tenant_id=..., tenant_name=...)`

**Step 1: Find all direct TenantMembership creations**

```bash
grep -rn "TenantMembership.objects.create" tests/ apps/
```

**Step 2: Update each creation to use `Tenant` first**

Pattern: wherever you see:
```python
TenantMembership.objects.create(user=user, provider="commcare", tenant_id="x", tenant_name="X")
```

Replace with:
```python
from apps.users.models import Tenant, TenantMembership
tenant = Tenant.objects.create(provider="commcare", external_id="x", canonical_name="X")
TenantMembership.objects.create(user=user, tenant=tenant)
```

Or for test files that have `setUpTestData`, create a shared `tenant` object alongside `workspace`.

**Step 3: Update `test_share_api.py`**

In `ArtifactShareAPITestCase.setUpTestData`, add `Tenant` creation:
```python
from apps.users.models import Tenant, TenantMembership

cls.tenant = Tenant.objects.create(
    provider="commcare", external_id="test-domain", canonical_name="Test Domain"
)
cls.workspace = TenantWorkspace.objects.create(tenant=cls.tenant)
# Any membership needed for access checks:
cls.creator_membership = TenantMembership.objects.create(user=cls.creator, tenant=cls.tenant)
cls.other_membership = TenantMembership.objects.create(user=cls.other_user, tenant=cls.tenant)
```

**Step 4: Run artifact tests**

```bash
uv run pytest apps/artifacts/tests/ -v
```
Expected: All PASS

**Step 5: Run full suite**

```bash
uv run pytest tests/ apps/ --ignore=tests/smoke -v
```
Expected: All PASS

**Step 6: Commit**

```bash
git add -u
git commit -m "test: update all test factories to use Tenant model (#52)"
```

---

### Task 12: Final verification and cleanup

**Step 1: Run full test suite**

```bash
uv run pytest tests/ apps/ --ignore=tests/smoke -v
```
Expected: All PASS, zero skips from model issues

**Step 2: Run linter**

```bash
uv run ruff check .
uv run ruff format --check .
```
Fix any issues.

**Step 3: Verify no raw `tenant_id=` string filter remains in auth-critical paths**

```bash
grep -rn "tenant_id=.*workspace\.tenant" apps/
```
Should return nothing — all switched to `tenant=workspace.tenant`.

**Step 4: Final commit (lint fixes only if needed)**

```bash
git add -u
git commit -m "chore: lint cleanup for canonical Tenant model refactor (#52)"
```

---

## Summary of files changed

| File | Change |
|------|--------|
| `apps/users/models.py` | Add `Tenant`; update `TenantMembership` to FK |
| `apps/users/migrations/0005_tenant.py` | New — `Tenant` table |
| `apps/users/migrations/0006_tenantmembership_use_tenant_fk.py` | New — update `TenantMembership` |
| `apps/users/services/tenant_resolution.py` | Upsert `Tenant` before `TenantMembership` |
| `apps/users/services/tenant_verification.py` | New — CommCare API-key verification |
| `apps/users/views.py` | Credential endpoint: verify before creating `Tenant` |
| `apps/projects/models.py` | Update `TenantWorkspace` to OneToOneField on `Tenant` |
| `apps/projects/migrations/0016_tenantworkspace_use_tenant_fk.py` | New — update `TenantWorkspace` |
| `apps/projects/api/views.py` | FK-based workspace resolution |
| `apps/projects/admin.py` | Update search fields |
| `apps/projects/services/schema_manager.py` | Use `tenant.external_id` |
| `apps/artifacts/views.py` | FK-based auth checks |
| `apps/recipes/api/views.py` | FK-based workspace resolution |
| `apps/knowledge/api/views.py` | FK-based workspace resolution |
| `apps/agents/graph/base.py` | FK-based workspace resolution |
| `tests/conftest.py` | Add `tenant` fixture, update `tenant_membership`, `workspace` |
| `tests/test_tenant_models.py` | Tests for new model shape |
| `tests/test_tenant_api.py` | Security tests for credential endpoint |
| `tests/test_tenant_verification.py` | New — verification service tests |
| `apps/artifacts/tests/test_share_api.py` | Updated factories |
| `apps/artifacts/tests/test_artifact_query_data.py` | Updated factories |
