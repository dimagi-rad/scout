# API Key Auth Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let users log into Scout with email/password and provide CommCare API keys instead of OAuth tokens for data materialization.

**Architecture:** Add a `TenantCredential` model (one-to-one with `TenantMembership`) that stores either `"oauth"` (pointer to allauth SocialToken) or `"api_key"` (Fernet-encrypted opaque string). The `run_materialization` MCP tool already does its own credential lookup from the DB — we update it to consult `TenantCredential` and choose the right auth header. A new `POST /api/auth/signup/` endpoint enables account creation without OAuth. An `OnboardingWizard` frontend component presents the two credential paths on first login.

**Tech Stack:** Django 5, django-allauth, cryptography (Fernet), React 19 + TypeScript, Zustand, React Router v6.

---

### Task 1: TenantCredential model + migration

**Files:**
- Modify: `apps/users/models.py`
- Modify: `apps/users/adapters.py` (add standalone encrypt/decrypt helpers)
- Create: `apps/users/migrations/00XX_add_tenantcredential.py` (auto-generated)

**Step 1: Write the failing test**

Add to `tests/test_users.py` (create file if absent):

```python
import pytest
from django.contrib.auth import get_user_model
from apps.users.models import TenantMembership, TenantCredential

User = get_user_model()

@pytest.fixture
def user(db):
    return User.objects.create_user(email="dev@example.com", password="pass1234")

@pytest.fixture
def membership(user):
    return TenantMembership.objects.create(
        user=user,
        provider="commcare",
        tenant_id="test-domain",
        tenant_name="Test Domain",
    )


class TestTenantCredential:
    def test_api_key_credential_fields(self, membership):
        cred = TenantCredential.objects.create(
            tenant_membership=membership,
            credential_type=TenantCredential.API_KEY,
            encrypted_credential="someencryptedvalue",
        )
        assert cred.pk is not None
        assert cred.credential_type == "api_key"

    def test_oauth_credential_fields(self, membership):
        cred = TenantCredential.objects.create(
            tenant_membership=membership,
            credential_type=TenantCredential.OAUTH,
        )
        assert cred.credential_type == "oauth"
        assert cred.encrypted_credential == ""

    def test_one_to_one_with_membership(self, membership):
        TenantCredential.objects.create(
            tenant_membership=membership,
            credential_type=TenantCredential.OAUTH,
        )
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            TenantCredential.objects.create(
                tenant_membership=membership,
                credential_type=TenantCredential.OAUTH,
            )
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_users.py::TestTenantCredential -v
```
Expected: FAIL — `ImportError: cannot import name 'TenantCredential'`

**Step 3: Add TenantCredential to models.py**

In `apps/users/models.py`, after the `TenantMembership` class (line 109), add:

```python
class TenantCredential(models.Model):
    """Stores credentials for a tenant — either OAuth pointer or encrypted API key.

    For credential_type == OAUTH: encrypted_credential is blank; the actual
    token lives in allauth's SocialToken and is retrieved from there.

    For credential_type == API_KEY: encrypted_credential holds a Fernet-encrypted
    opaque string. Format is provider-specific, e.g. "username:apikey" for CommCare.
    """

    OAUTH = "oauth"
    API_KEY = "api_key"
    TYPE_CHOICES = [
        (OAUTH, "OAuth Token"),
        (API_KEY, "API Key"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_membership = models.OneToOneField(
        TenantMembership,
        on_delete=models.CASCADE,
        related_name="credential",
    )
    credential_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    encrypted_credential = models.CharField(
        max_length=2000,
        blank=True,
        help_text="Fernet-encrypted opaque string. Empty for OAuth type.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant_membership} ({self.credential_type})"
```

**Step 4: Add encrypt/decrypt helpers to adapters.py**

In `apps/users/adapters.py`, add after the `EncryptingSocialAccountAdapter` class:

```python
def encrypt_credential(plaintext: str) -> str:
    """Fernet-encrypt a credential string using DB_CREDENTIAL_KEY."""
    from django.conf import settings

    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str) -> str:
    """Fernet-decrypt a credential string using DB_CREDENTIAL_KEY."""
    from django.conf import settings

    key = settings.DB_CREDENTIAL_KEY
    if not key:
        raise ValueError("DB_CREDENTIAL_KEY is not set in settings")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(ciphertext.encode()).decode()
```

**Step 5: Generate and run migration**

```bash
uv run python manage.py makemigrations users --name add_tenantcredential
uv run python manage.py migrate
```

**Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_users.py::TestTenantCredential -v
```
Expected: 3 tests PASS

**Step 7: Commit**

```bash
git add apps/users/models.py apps/users/adapters.py apps/users/migrations/
git commit -m "feat(users): add TenantCredential model with Fernet helpers"
```

---

### Task 2: Auto-create TenantCredential(oauth) when OAuth resolves domains

**Files:**
- Modify: `apps/users/services/tenant_resolution.py`
- Test: `tests/test_users.py`

**Step 1: Write the failing test**

Add to `tests/test_users.py`:

```python
class TestResolveCommcareDomains:
    def test_creates_tenant_credential_oauth(self, user, db):
        """resolve_commcare_domains must create TenantCredential(type=oauth) for each membership."""
        from unittest.mock import patch
        from apps.users.services.tenant_resolution import resolve_commcare_domains
        from apps.users.models import TenantCredential

        fake_domains = [
            {"domain_name": "domain-a", "project_name": "Domain A"},
            {"domain_name": "domain-b", "project_name": "Domain B"},
        ]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            return_value=fake_domains,
        ):
            memberships = resolve_commcare_domains(user, "fake-token")

        assert len(memberships) == 2
        for tm in memberships:
            cred = TenantCredential.objects.get(tenant_membership=tm)
            assert cred.credential_type == TenantCredential.OAUTH
            assert cred.encrypted_credential == ""

    def test_idempotent_on_re_resolve(self, user, db):
        """Calling resolve twice does not create duplicate TenantCredentials."""
        from unittest.mock import patch
        from apps.users.services.tenant_resolution import resolve_commcare_domains
        from apps.users.models import TenantCredential

        fake_domains = [{"domain_name": "domain-a", "project_name": "Domain A"}]
        with patch(
            "apps.users.services.tenant_resolution._fetch_all_domains",
            return_value=fake_domains,
        ):
            resolve_commcare_domains(user, "fake-token")
            resolve_commcare_domains(user, "fake-token")

        assert TenantCredential.objects.filter(
            tenant_membership__user=user
        ).count() == 1
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_users.py::TestResolveCommcareDomains -v
```
Expected: FAIL — `TenantCredential.DoesNotExist`

**Step 3: Update tenant_resolution.py**

In `apps/users/services/tenant_resolution.py`, update the `resolve_commcare_domains` function:

```python
from apps.users.models import TenantCredential, TenantMembership

def resolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and upsert TenantMembership records."""
    domains = _fetch_all_domains(access_token)
    memberships = []

    for domain in domains:
        tm, _created = TenantMembership.objects.update_or_create(
            user=user,
            provider="commcare",
            tenant_id=domain["domain_name"],
            defaults={"tenant_name": domain["project_name"]},
        )
        # Ensure a TenantCredential(oauth) exists for this membership
        TenantCredential.objects.get_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)

    logger.info(
        "Resolved %d CommCare domains for user %s",
        len(memberships),
        user.email,
    )
    return memberships
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_users.py::TestResolveCommcareDomains -v
```
Expected: 2 tests PASS

**Step 5: Commit**

```bash
git add apps/users/services/tenant_resolution.py tests/test_users.py
git commit -m "feat(users): auto-create TenantCredential on OAuth domain resolution"
```

---

### Task 3: Signup endpoint

**Files:**
- Modify: `apps/chat/views.py`
- Modify: `apps/chat/auth_urls.py`
- Test: `tests/test_auth.py`

**Step 1: Write the failing test**

Add to `tests/test_auth.py`:

```python
class TestSignup:
    def test_signup_creates_user_and_logs_in(self, client, db):
        response = client.post(
            "/api/auth/signup/",
            data={"email": "new@example.com", "password": "str0ngPass!"},
            content_type="application/json",
        )
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "new@example.com"

        # Should be logged in — me/ returns 200
        me = client.get("/api/auth/me/")
        assert me.status_code == 200

    def test_signup_duplicate_email_returns_400(self, client, db):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        User.objects.create_user(email="existing@example.com", password="pass")

        response = client.post(
            "/api/auth/signup/",
            data={"email": "existing@example.com", "password": "newpass"},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_signup_missing_fields_returns_400(self, client, db):
        response = client.post(
            "/api/auth/signup/",
            data={"email": "x@example.com"},
            content_type="application/json",
        )
        assert response.status_code == 400
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_auth.py::TestSignup -v
```
Expected: FAIL — 404 (route not found)

**Step 3: Add signup_view to apps/chat/views.py**

Add after `logout_view` (around line 170):

```python
@require_POST
def signup_view(request):
    """Create a new account with email and password, then log in."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return JsonResponse({"error": "Email and password are required"}, status=400)

    from django.contrib.auth import get_user_model
    User = get_user_model()

    if User.objects.filter(email=email).exists():
        return JsonResponse({"error": "An account with this email already exists"}, status=400)

    user = User.objects.create_user(email=email, password=password)
    login(request, user)

    return JsonResponse(
        {
            "id": str(user.id),
            "email": user.email,
            "name": user.get_full_name(),
            "is_staff": user.is_staff,
        },
        status=201,
    )
```

**Step 4: Register route in apps/chat/auth_urls.py**

Add import and path:

```python
from apps.chat.views import (
    csrf_view,
    disconnect_provider_view,
    login_view,
    logout_view,
    me_view,
    providers_view,
    signup_view,          # add this
)

urlpatterns = [
    # ... existing paths ...
    path("signup/", signup_view, name="signup"),
]
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_auth.py::TestSignup -v
```
Expected: 3 tests PASS

**Step 6: Commit**

```bash
git add apps/chat/views.py apps/chat/auth_urls.py tests/test_auth.py
git commit -m "feat(auth): add POST /api/auth/signup/ endpoint"
```

---

### Task 4: onboarding_complete flag in me_view

**Files:**
- Modify: `apps/chat/views.py` (`me_view`)
- Test: `tests/test_auth.py`

**Step 1: Write the failing test**

Add to `tests/test_auth.py`:

```python
from django.contrib.auth import get_user_model
from apps.users.models import TenantMembership, TenantCredential

User = get_user_model()


class TestMeOnboardingComplete:
    def test_false_with_no_memberships(self, client, db):
        user = User.objects.create_user(email="u@example.com", password="pass")
        client.force_login(user)
        resp = client.get("/api/auth/me/")
        assert resp.status_code == 200
        assert resp.json()["onboarding_complete"] is False

    def test_true_with_membership_and_credential(self, client, db):
        user = User.objects.create_user(email="u2@example.com", password="pass")
        tm = TenantMembership.objects.create(
            user=user,
            provider="commcare",
            tenant_id="d1",
            tenant_name="D1",
        )
        TenantCredential.objects.create(
            tenant_membership=tm,
            credential_type=TenantCredential.OAUTH,
        )
        client.force_login(user)
        resp = client.get("/api/auth/me/")
        assert resp.json()["onboarding_complete"] is True
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_auth.py::TestMeOnboardingComplete -v
```
Expected: FAIL — `KeyError: 'onboarding_complete'`

**Step 3: Update me_view in apps/chat/views.py**

Replace the `me_view` function (lines 114-127):

```python
@require_GET
def me_view(request):
    """Return current user info or 401."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Not authenticated"}, status=401)
    user = request.user

    from apps.users.models import TenantCredential, TenantMembership

    onboarding_complete = TenantMembership.objects.filter(
        user=user,
        credential__isnull=False,
    ).exists()

    return JsonResponse(
        {
            "id": str(user.id),
            "email": user.email,
            "name": user.get_full_name(),
            "is_staff": user.is_staff,
            "onboarding_complete": onboarding_complete,
        }
    )
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_auth.py::TestMeOnboardingComplete -v
```
Expected: 2 tests PASS

**Step 5: Commit**

```bash
git add apps/chat/views.py tests/test_auth.py
git commit -m "feat(auth): add onboarding_complete to /api/auth/me/ response"
```

---

### Task 5: Tenant credential API endpoints

**Files:**
- Modify: `apps/users/views.py`
- Modify: `apps/chat/auth_urls.py`
- Test: `tests/test_users.py`

**Step 1: Write the failing tests**

Add to `tests/test_users.py`:

```python
class TestTenantCredentialEndpoints:
    def test_post_creates_membership_and_credential(self, client, db, user):
        client.force_login(user)
        resp = client.post(
            "/api/auth/tenant-credentials/",
            data={
                "provider": "commcare",
                "tenant_id": "my-domain",
                "tenant_name": "My Domain",
                "credential": "user@example.com:abc123",
            },
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "membership_id" in data

        from apps.users.models import TenantCredential, TenantMembership
        tm = TenantMembership.objects.get(id=data["membership_id"])
        assert tm.provider == "commcare"
        assert tm.tenant_id == "my-domain"
        cred = TenantCredential.objects.get(tenant_membership=tm)
        assert cred.credential_type == TenantCredential.API_KEY

    def test_api_key_stored_encrypted(self, client, db, user):
        """The raw DB value must not contain the plaintext credential."""
        client.force_login(user)
        plaintext = "user@example.com:supersecretkey"
        client.post(
            "/api/auth/tenant-credentials/",
            data={
                "provider": "commcare",
                "tenant_id": "secure-domain",
                "tenant_name": "Secure Domain",
                "credential": plaintext,
            },
            content_type="application/json",
        )
        from apps.users.models import TenantCredential
        cred = TenantCredential.objects.get(
            tenant_membership__tenant_id="secure-domain"
        )
        assert plaintext not in cred.encrypted_credential
        # Verify round-trip decryption works
        from apps.users.adapters import decrypt_credential
        assert decrypt_credential(cred.encrypted_credential) == plaintext

    def test_get_lists_credentials(self, client, db, user):
        from apps.users.models import TenantCredential, TenantMembership
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="d1", tenant_name="D1"
        )
        TenantCredential.objects.create(
            tenant_membership=tm, credential_type=TenantCredential.OAUTH
        )
        client.force_login(user)
        resp = client.get("/api/auth/tenant-credentials/")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["credential_type"] == "oauth"
        assert "encrypted_credential" not in items[0]  # never exposed

    def test_delete_removes_credential_and_membership(self, client, db, user):
        from apps.users.models import TenantCredential, TenantMembership
        tm = TenantMembership.objects.create(
            user=user, provider="commcare", tenant_id="d2", tenant_name="D2"
        )
        TenantCredential.objects.create(
            tenant_membership=tm, credential_type=TenantCredential.OAUTH
        )
        client.force_login(user)
        resp = client.delete(f"/api/auth/tenant-credentials/{tm.id}/")
        assert resp.status_code == 200
        assert not TenantMembership.objects.filter(id=tm.id).exists()

    def test_unauthenticated_returns_401(self, client, db):
        resp = client.post("/api/auth/tenant-credentials/", data={}, content_type="application/json")
        assert resp.status_code == 401
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_users.py::TestTenantCredentialEndpoints -v
```
Expected: FAIL — 404

**Step 3: Add views to apps/users/views.py**

Append to `apps/users/views.py`:

```python
@require_http_methods(["GET", "POST"])
async def tenant_credential_list_view(request):
    """GET  /api/auth/tenant-credentials/ — list configured tenant credentials
    POST /api/auth/tenant-credentials/ — create a new API-key-based tenant"""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    if request.method == "GET":
        results = []
        async for tm in TenantMembership.objects.filter(
            user=user,
            credential__isnull=False,
        ).select_related("credential"):
            results.append(
                {
                    "membership_id": str(tm.id),
                    "provider": tm.provider,
                    "tenant_id": tm.tenant_id,
                    "tenant_name": tm.tenant_name,
                    "credential_type": tm.credential.credential_type,
                }
            )
        return JsonResponse(results, safe=False)

    # POST — create API-key-backed membership
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

    from django.db import transaction

    from apps.users.adapters import encrypt_credential
    from apps.users.models import TenantCredential

    try:
        encrypted = await sync_to_async(encrypt_credential)(credential)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=500)

    def _create():
        with transaction.atomic():
            tm, _ = TenantMembership.objects.update_or_create(
                user=user,
                provider=provider,
                tenant_id=tenant_id,
                defaults={"tenant_name": tenant_name},
            )
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


@require_http_methods(["DELETE"])
async def tenant_credential_detail_view(request, membership_id):
    """DELETE /api/auth/tenant-credentials/<membership_id>/ — remove a credential"""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    def _delete():
        try:
            tm = TenantMembership.objects.get(id=membership_id, user=user)
            tm.delete()  # cascades to TenantCredential
            return True
        except TenantMembership.DoesNotExist:
            return False

    deleted = await sync_to_async(_delete)()
    if not deleted:
        return JsonResponse({"error": "Not found"}, status=404)
    return JsonResponse({"status": "deleted"})
```

**Step 4: Register routes in apps/chat/auth_urls.py**

Add imports and paths:

```python
from apps.users.views import (
    tenant_credential_detail_view,
    tenant_credential_list_view,
    tenant_list_view,
    tenant_select_view,
)

urlpatterns = [
    # ... existing ...
    path("tenant-credentials/", tenant_credential_list_view, name="tenant-credential-list"),
    path(
        "tenant-credentials/<str:membership_id>/",
        tenant_credential_detail_view,
        name="tenant-credential-detail",
    ),
]
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_users.py::TestTenantCredentialEndpoints -v
```
Expected: 5 tests PASS

**Step 6: Commit**

```bash
git add apps/users/views.py apps/chat/auth_urls.py tests/test_users.py
git commit -m "feat(users): add tenant-credentials CRUD endpoints"
```

---

### Task 6: Update CommCareCaseLoader and run_materialization for API key auth

**Files:**
- Modify: `mcp_server/loaders/commcare_cases.py`
- Modify: `mcp_server/services/materializer.py`
- Modify: `mcp_server/server.py` (run_materialization tool)
- Test: `tests/test_mcp_server.py`

**Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestCommCareCaseLoaderAuth:
    def test_uses_bearer_header_for_oauth(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader
        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            json={"cases": [], "next": None},
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "oauth", "value": "mytoken"},
        )
        loader.load()
        assert requests_mock.last_request.headers["Authorization"] == "Bearer mytoken"

    def test_uses_apikey_header_for_api_key(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareCaseLoader
        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            json={"cases": [], "next": None},
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "api_key", "value": "user@example.com:abc123"},
        )
        loader.load()
        assert requests_mock.last_request.headers["Authorization"] == "ApiKey user@example.com:abc123"

    def test_raises_auth_error_on_401(self, requests_mock):
        from mcp_server.loaders.commcare_cases import CommCareAuthError, CommCareCaseLoader
        requests_mock.get(
            "https://www.commcarehq.org/a/test-domain/api/case/v2/",
            status_code=401,
        )
        loader = CommCareCaseLoader(
            domain="test-domain",
            credential={"type": "api_key", "value": "user:key"},
        )
        with pytest.raises(CommCareAuthError):
            loader.load()
```

Note: `requests_mock` requires `pytest-requests-mock`. Check if it's installed:
```bash
uv run pytest --collect-only tests/test_mcp_server.py 2>&1 | grep "requests_mock"
```
If missing: `uv add --dev pytest-requests-mock`

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_server.py::TestCommCareCaseLoaderAuth -v
```
Expected: FAIL — `TypeError: __init__() got unexpected keyword argument 'credential'`

**Step 3: Update CommCareCaseLoader**

Replace `mcp_server/loaders/commcare_cases.py` entirely:

```python
"""CommCare case loader — fetches case data from the CommCare HQ Case API v2."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareAuthError(Exception):
    """Raised when the CommCare API rejects the credential (401/403)."""


class CommCareCaseLoader:
    """Loads case records from CommCare HQ using the Case API v2.

    The v2 API uses cursor-based pagination and returns cases serialized with
    fields like case_name, last_modified, indices, and properties.

    Args:
        domain: CommCare domain name.
        credential: Dict with keys "type" ("oauth" or "api_key") and "value".
            For oauth: value is a Bearer token string.
            For api_key: value is "username:apikey" string.

    See: https://commcare-hq.readthedocs.io/api/cases-v2.html
    """

    def __init__(
        self,
        domain: str,
        credential: dict[str, str],
        *,
        page_size: int = 1000,
        # Legacy parameter kept for backwards compatibility
        access_token: str | None = None,
    ):
        self.domain = domain
        if access_token is not None and not credential:
            # Legacy callers: wrap plain token as oauth credential
            credential = {"type": "oauth", "value": access_token}
        self.credential = credential
        self.page_size = min(page_size, 5000)  # API max is 5000
        self.base_url = f"{COMMCARE_API_BASE}/a/{domain}/api/case/v2/"

    def _auth_header(self) -> str:
        cred_type = self.credential.get("type", "oauth")
        value = self.credential.get("value", "")
        if cred_type == "api_key":
            return f"ApiKey {value}"
        return f"Bearer {value}"

    def load(self) -> list[dict]:
        """Fetch all cases from the CommCare Case API v2 (cursor-paginated)."""
        results: list[dict] = []
        url = self.base_url
        params = {"limit": self.page_size}

        while url:
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": self._auth_header()},
                timeout=60,
            )
            if resp.status_code in (401, 403):
                raise CommCareAuthError(
                    f"CommCare returned {resp.status_code} — the credential may be "
                    f"expired or invalid. Please reconnect your CommCare account."
                )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("cases", []))

            # Cursor pagination: follow the "next" URL if present
            url = data.get("next")
            params = {}  # next URL includes all params

            logger.info(
                "Loaded %d/%s cases for domain %s",
                len(results),
                data.get("matching_records", "?"),
                self.domain,
            )

        return results
```

**Step 4: Update materializer.py to accept credential dict**

In `mcp_server/services/materializer.py`, change the function signature and loader call:

```python
def run_commcare_sync(tenant_membership, credential: dict[str, str]) -> dict:
    """Load CommCare cases into the tenant's schema.

    Args:
        tenant_membership: The TenantMembership to sync.
        credential: Dict with "type" and "value" keys for auth.
    """
    # 1. Provision schema
    mgr = SchemaManager()
    tenant_schema = mgr.provision(tenant_membership)
    schema_name = tenant_schema.schema_name

    # 2. Load cases from CommCare (v2 API)
    loader = CommCareCaseLoader(
        domain=tenant_membership.tenant_id,
        credential=credential,
    )
    cases = loader.load()
    # ... rest of function unchanged ...
```

**Step 5: Update run_materialization in mcp_server/server.py**

Replace the credential-retrieval block (lines 278-293) with:

```python
        # Get credential from TenantCredential (supports both OAuth and API key)
        from apps.users.models import TenantCredential

        try:
            cred_obj = await TenantCredential.objects.select_related(
                "tenant_membership"
            ).aget(tenant_membership=tm)
        except TenantCredential.DoesNotExist:
            tc["result"] = error_response("AUTH_TOKEN_MISSING", "No credential configured for this tenant")
            return tc["result"]

        if cred_obj.credential_type == TenantCredential.API_KEY:
            from apps.users.adapters import decrypt_credential
            try:
                decrypted = await sync_to_async(decrypt_credential)(cred_obj.encrypted_credential)
            except Exception:
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "Failed to decrypt API key")
                return tc["result"]
            credential = {"type": "api_key", "value": decrypted}
        else:
            # OAuth: retrieve from allauth SocialToken
            from allauth.socialaccount.models import SocialToken
            token_obj = (
                await SocialToken.objects.filter(
                    account__user=tm.user,
                    account__provider__startswith="commcare",
                )
                .exclude(account__provider__startswith="commcare_connect")
                .afirst()
            )
            if not token_obj:
                tc["result"] = error_response("AUTH_TOKEN_MISSING", "No CommCare OAuth token found")
                return tc["result"]
            credential = {"type": "oauth", "value": token_obj.token}
```

And update the call to `run_commcare_sync`:

```python
        result = await sync_to_async(run_commcare_sync)(tm, credential)
```

**Step 6: Run tests**

```bash
uv run pytest tests/test_mcp_server.py::TestCommCareCaseLoaderAuth -v
```
Expected: 3 tests PASS

Also verify existing tests still pass:
```bash
uv run pytest tests/ -v --tb=short
```

**Step 7: Commit**

```bash
git add mcp_server/loaders/commcare_cases.py mcp_server/services/materializer.py mcp_server/server.py tests/test_mcp_server.py
git commit -m "feat(mcp): support ApiKey auth in CommCareCaseLoader and run_materialization"
```

---

### Task 7: Frontend — onboarding_complete in auth store

**Files:**
- Modify: `frontend/src/store/authSlice.ts`

**Step 1: Update User type and fetchMe in authSlice.ts**

In `frontend/src/store/authSlice.ts`, update the `User` interface:

```typescript
export interface User {
  id: string
  email: string
  name: string
  is_staff: boolean
  onboarding_complete: boolean
}
```

No other changes needed — `fetchMe` already stores the full API response in `user`, and the new field will come through automatically.

**Step 2: Verify TypeScript compiles**

```bash
cd frontend && bun run build 2>&1 | head -40
```
Expected: build succeeds (or only pre-existing errors)

**Step 3: Commit**

```bash
git add frontend/src/store/authSlice.ts
git commit -m "feat(frontend): add onboarding_complete to User type"
```

---

### Task 8: Frontend — OnboardingWizard component

**Files:**
- Create: `frontend/src/components/OnboardingWizard/OnboardingWizard.tsx`
- Modify: `frontend/src/App.tsx`

**Step 1: Create OnboardingWizard.tsx**

Create `frontend/src/components/OnboardingWizard/OnboardingWizard.tsx`:

```tsx
import { useState, type FormEvent } from "react"
import { useAppStore } from "@/store/store"
import { api } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

type Step = "choose" | "api-key"

export function OnboardingWizard() {
  const [step, setStep] = useState<Step>("choose")
  const [domain, setDomain] = useState("")
  const [username, setUsername] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)

  async function handleApiKeySubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await api.post("/api/auth/tenant-credentials/", {
        provider: "commcare",
        tenant_id: domain,
        tenant_name: domain,
        credential: `${username}:${apiKey}`,
      })
      // Refresh auth state so onboarding_complete becomes true
      await fetchMe()
      await fetchDomains()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save credentials")
    } finally {
      setLoading(false)
    }
  }

  if (step === "api-key") {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle>Connect with API Key</CardTitle>
            <CardDescription>
              Find your API key in CommCare under Settings → My Account → API Key.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleApiKeySubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="domain">CommCare Domain</Label>
                <Input
                  id="domain"
                  data-testid="onboarding-domain"
                  required
                  placeholder="my-project"
                  value={domain}
                  onChange={(e) => setDomain(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="username">CommCare Username</Label>
                <Input
                  id="username"
                  data-testid="onboarding-username"
                  type="email"
                  required
                  placeholder="you@example.com"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="api-key">API Key</Label>
                <Input
                  id="api-key"
                  data-testid="onboarding-api-key"
                  type="password"
                  required
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </div>
              {error && <p className="text-sm text-destructive">{error}</p>}
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="flex-1"
                  onClick={() => setStep("choose")}
                >
                  Back
                </Button>
                <Button type="submit" className="flex-1" disabled={loading}>
                  {loading ? "Connecting..." : "Connect"}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      </div>
    )
  }

  // step === "choose"
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle>Connect your CommCare data</CardTitle>
          <CardDescription>
            Choose how to connect Scout to your CommCare account.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button
            className="w-full"
            variant="outline"
            data-testid="onboarding-oauth"
            asChild
          >
            <a href="/accounts/commcare/login/?next=/">Connect with OAuth</a>
          </Button>
          <Button
            className="w-full"
            data-testid="onboarding-api-key-option"
            onClick={() => setStep("api-key")}
          >
            Use an API Key
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
```

**Step 2: Update App.tsx to show wizard when onboarding is incomplete**

In `frontend/src/App.tsx`, update the authenticated state check:

```tsx
import { OnboardingWizard } from "@/components/OnboardingWizard/OnboardingWizard"

// Inside the App() function, replace the final return block:

  if (authStatus === "unauthenticated") {
    return <LoginForm />
  }

  // authenticated — check onboarding
  if (authStatus === "authenticated" && user && !user.onboarding_complete) {
    return <OnboardingWizard />
  }

  return <RouterProvider router={router} />
```

**Step 3: Verify TypeScript compiles**

```bash
cd frontend && bun run build 2>&1 | head -40
```
Expected: build succeeds

**Step 4: Commit**

```bash
git add frontend/src/components/OnboardingWizard/ frontend/src/App.tsx
git commit -m "feat(frontend): add OnboardingWizard for first-login credential setup"
```

---

### Task 9: Full test run + lint

**Step 1: Run all backend tests**

```bash
uv run pytest tests/ -v --tb=short
```
Expected: all pass

**Step 2: Run frontend lint**

```bash
cd frontend && bun run lint
```
Expected: no errors

**Step 3: Run Python linter**

```bash
uv run ruff check .
uv run ruff format --check .
```
Expected: no errors. If format issues: `uv run ruff format .`

**Step 4: Final commit if any lint fixes**

```bash
git add -u
git commit -m "chore: fix lint issues"
```
