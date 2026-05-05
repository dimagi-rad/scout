# API Key Providers (CommCare + OCS) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Generalize CommCare API-key authentication into a provider-strategy registry so OCS (and later Connect) can be connected via personal API keys.

**Architecture:** Introduce `apps/users/services/api_key_providers/` containing a `CredentialProviderStrategy` base class plus one concrete strategy per provider. The existing `tenant_credential_list_view` POST/PATCH endpoints dispatch through the registry. A new `GET /api/auth/api-key-providers/` endpoint exposes the form schemas so the frontend dialog can render dynamically. Loader auth headers stay in `mcp_server/loaders/*_base.py` and dispatch on `credential["type"]` — no cross-app coupling.

**Tech Stack:** Django 5 (async views), pytest + pytest-asyncio + pytest-httpx (httpx_mock fixture), React 19 + TypeScript + Tailwind, Fernet-encrypted credentials.

**Design doc:** `docs/plans/2026-04-30-api-key-providers-design.md`

---

## Background context the executor needs

Read these files before starting any task — they define the patterns to follow:

- `apps/users/models.py:81-198` — `Tenant`, `TenantMembership`, `TenantCredential` models.
- `apps/users/services/tenant_verification.py` — existing CommCare verification (the pattern OCSStrategy follows).
- `apps/users/services/tenant_resolution.py:106-150` — `resolve_ocs_chatbots()` shows how to paginate `/api/experiments/`.
- `apps/users/views.py:129-271` — `tenant_credential_list_view` and `tenant_credential_detail_view` (the views being refactored).
- `apps/users/auth_urls.py` — URL patterns to extend.
- `mcp_server/loaders/commcare_base.py` — auth-header dispatch pattern to mirror in OCS.
- `mcp_server/loaders/ocs_base.py` — file being changed in Task 8.
- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` — page being refactored.
- `tests/test_tenant_verification.py` — example of `httpx_mock` usage in this repo.
- `tests/test_users.py:99-189` and `tests/test_tenant_api.py:80-160` — existing test cases for the credential endpoints; their request shapes change in Tasks 6 and 7.

**OCS API specifics** (from <https://developers.openchatstudio.com/developer_guides/api_documentation/>):
- API key header: `X-api-key: <key>` (note the lowercase `api-key`).
- OAuth header: `Authorization: Bearer <token>`.
- Verification endpoint: `GET ${OCS_URL}/api/experiments/`. Response shape: `{"results": [{"id": str, "name": str, ...}, ...], "next": <absolute_url_or_null>}`.
- `OCS_URL` is read from Django settings via `getattr(settings, "OCS_URL", "https://www.openchatstudio.com")`.

**Project conventions reminders:**
- Async ORM only inside async views: `aget`, `acreate`, `aupdate_or_create`, `async for`. No `sync_to_async` for ORM.
- Module-level imports only (no inline imports). Test files may use lazy imports.
- Run lints before commit: `uv run ruff check .` and `uv run ruff format .`.
- Use `uv run pytest <path>` to run individual tests.
- Async DB tests need `@pytest.mark.django_db(transaction=True)` plus `@pytest.mark.asyncio`.

---

## Task 1: Scaffold the strategy module

**Files:**
- Create: `apps/users/services/api_key_providers/__init__.py`
- Create: `apps/users/services/api_key_providers/base.py`

**Step 1: Create the package init**

```python
# apps/users/services/api_key_providers/__init__.py
"""Provider-strategy abstraction for API-key authentication.

Each concrete strategy describes how to verify a personal API key for a
provider (CommCare, OCS, Connect) and discover the tenant(s) that key
grants access to. The strategy registry in registry.py maps provider IDs
to strategy classes; views and the frontend dialog dispatch through it.
"""

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)

__all__ = [
    "CredentialProviderStrategy",
    "CredentialVerificationError",
    "FormField",
    "TenantDescriptor",
]
```

**Step 2: Create the base module**

```python
# apps/users/services/api_key_providers/base.py
"""Base types for the API-key provider strategy registry."""

from __future__ import annotations

from typing import NamedTuple, TypedDict


class TenantDescriptor(NamedTuple):
    """A tenant the credential grants access to."""

    external_id: str
    canonical_name: str


class FormField(TypedDict):
    """A field in the Add/Edit dialog form schema."""

    key: str
    label: str
    type: str  # "text" | "password"
    required: bool
    editable_on_rotate: bool


class CredentialVerificationError(Exception):
    """Raised when the provider rejects a credential or the tenant is not accessible."""


class CredentialProviderStrategy:
    """Strategy for an API-key-authenticated provider.

    Subclasses set the class attributes and implement the four classmethods.
    All network IO lives in verify_and_discover and verify_for_tenant.
    """

    provider_id: str = ""
    display_name: str = ""
    form_fields: list[FormField] = []

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        """Serialize form fields into the opaque encrypted_credential string."""
        raise NotImplementedError

    @classmethod
    async def verify_and_discover(
        cls, fields: dict[str, str]
    ) -> list[TenantDescriptor]:
        """Verify the credential and return all tenants it grants access to.

        Raises CredentialVerificationError on failure.
        """
        raise NotImplementedError

    @classmethod
    async def verify_for_tenant(
        cls, fields: dict[str, str], external_id: str
    ) -> None:
        """Verify the credential still grants access to a known tenant.

        Used during PATCH (key rotation). Raises CredentialVerificationError
        on failure.
        """
        raise NotImplementedError
```

**Step 3: Verify it imports**

Run: `uv run python -c "from apps.users.services.api_key_providers import CredentialProviderStrategy; print('ok')"`
Expected: `ok`

**Step 4: Lint and commit**

```bash
uv run ruff check apps/users/services/api_key_providers/
uv run ruff format apps/users/services/api_key_providers/
git add apps/users/services/api_key_providers/
git commit -m "feat(auth): scaffold api_key_providers strategy module"
```

---

## Task 2: CommCareStrategy (TDD)

**Files:**
- Create: `apps/users/services/api_key_providers/commcare.py`
- Create: `tests/test_api_key_strategies_commcare.py`

**Step 1: Write the failing tests**

```python
# tests/test_api_key_strategies_commcare.py
import pytest

from apps.users.services.api_key_providers import CredentialVerificationError


def test_pack_credential_joins_username_and_key():
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    packed = CommCareStrategy.pack_credential(
        {"domain": "dimagi", "username": "user@d.org", "api_key": "secret"}
    )
    assert packed == "user@d.org:secret"


def test_form_fields_metadata():
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    keys = [f["key"] for f in CommCareStrategy.form_fields]
    assert keys == ["domain", "username", "api_key"]
    assert CommCareStrategy.provider_id == "commcare"
    # domain is not editable on key rotation; username + api_key are
    by_key = {f["key"]: f for f in CommCareStrategy.form_fields}
    assert by_key["domain"]["editable_on_rotate"] is False
    assert by_key["username"]["editable_on_rotate"] is True
    assert by_key["api_key"]["editable_on_rotate"] is True


@pytest.mark.asyncio
async def test_verify_and_discover_happy_path(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "dimagi", "project_name": "Dimagi Inc"}]},
        status_code=200,
    )
    descriptors = await CommCareStrategy.verify_and_discover(
        {"domain": "dimagi", "username": "user@d.org", "api_key": "k"}
    )
    assert descriptors == [("dimagi", "dimagi")]
    request = httpx_mock.get_request()
    assert request.headers["Authorization"] == "ApiKey user@d.org:k"


@pytest.mark.asyncio
async def test_verify_and_discover_unauthorized(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        status_code=401,
    )
    with pytest.raises(CredentialVerificationError):
        await CommCareStrategy.verify_and_discover(
            {"domain": "dimagi", "username": "u", "api_key": "k"}
        )


@pytest.mark.asyncio
async def test_verify_and_discover_domain_not_in_list(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "other"}]},
        status_code=200,
    )
    with pytest.raises(CredentialVerificationError, match="not a member"):
        await CommCareStrategy.verify_and_discover(
            {"domain": "dimagi", "username": "u", "api_key": "k"}
        )


@pytest.mark.asyncio
async def test_verify_for_tenant_calls_verify_with_external_id(httpx_mock):
    from apps.users.services.api_key_providers.commcare import CommCareStrategy

    httpx_mock.add_response(
        method="GET",
        url="https://www.commcarehq.org/api/user_domains/v1/",
        json={"objects": [{"domain_name": "dimagi"}]},
        status_code=200,
    )
    # Should not raise. Note: form fields on PATCH may omit `domain`; the
    # external_id passed in plays the role of the domain to verify.
    await CommCareStrategy.verify_for_tenant(
        {"username": "u", "api_key": "k"}, external_id="dimagi"
    )
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_key_strategies_commcare.py -v`
Expected: All FAIL with `ModuleNotFoundError: apps.users.services.api_key_providers.commcare`.

**Step 3: Implement CommCareStrategy**

```python
# apps/users/services/api_key_providers/commcare.py
"""CommCare HQ API-key strategy."""

from __future__ import annotations

import httpx

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)

COMMCARE_API_BASE = "https://www.commcarehq.org"
COMMCARE_DOMAINS_URL = f"{COMMCARE_API_BASE}/api/user_domains/v1/"


def _auth_header(username: str, api_key: str) -> dict[str, str]:
    return {"Authorization": f"ApiKey {username}:{api_key}"}


async def _list_domains(username: str, api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(COMMCARE_DOMAINS_URL, headers=_auth_header(username, api_key))
    if resp.status_code in (401, 403):
        raise CredentialVerificationError(
            f"CommCare rejected the API key (HTTP {resp.status_code})"
        )
    if not resp.is_success:
        raise CredentialVerificationError(
            f"CommCare API returned unexpected status {resp.status_code}"
        )
    return resp.json().get("objects", [])


class CommCareStrategy(CredentialProviderStrategy):
    provider_id = "commcare"
    display_name = "CommCare HQ"
    form_fields: list[FormField] = [
        {"key": "domain", "label": "Domain", "type": "text",
         "required": True, "editable_on_rotate": False},
        {"key": "username", "label": "Username", "type": "text",
         "required": True, "editable_on_rotate": True},
        {"key": "api_key", "label": "API Key", "type": "password",
         "required": True, "editable_on_rotate": True},
    ]

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return f"{fields['username']}:{fields['api_key']}"

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        domain = fields["domain"]
        domains = await _list_domains(fields["username"], fields["api_key"])
        for entry in domains:
            if entry.get("domain_name") == domain:
                return [TenantDescriptor(domain, domain)]
        raise CredentialVerificationError(
            f"User '{fields['username']}' is not a member of domain '{domain}'"
        )

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        domains = await _list_domains(fields["username"], fields["api_key"])
        for entry in domains:
            if entry.get("domain_name") == external_id:
                return
        raise CredentialVerificationError(
            f"API key does not have access to domain '{external_id}'"
        )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_key_strategies_commcare.py -v`
Expected: All PASS.

**Step 5: Lint and commit**

```bash
uv run ruff check apps/users/services/api_key_providers/ tests/test_api_key_strategies_commcare.py
uv run ruff format apps/users/services/api_key_providers/ tests/test_api_key_strategies_commcare.py
git add apps/users/services/api_key_providers/commcare.py tests/test_api_key_strategies_commcare.py
git commit -m "feat(auth): add CommCare API-key provider strategy"
```

---

## Task 3: OCSStrategy (TDD)

**Files:**
- Create: `apps/users/services/api_key_providers/ocs.py`
- Create: `tests/test_api_key_strategies_ocs.py`

**Step 1: Write the failing tests**

```python
# tests/test_api_key_strategies_ocs.py
import pytest

from apps.users.services.api_key_providers import CredentialVerificationError


def test_form_fields_only_api_key():
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    assert OCSStrategy.provider_id == "ocs"
    keys = [f["key"] for f in OCSStrategy.form_fields]
    assert keys == ["api_key"]
    assert OCSStrategy.form_fields[0]["editable_on_rotate"] is True


def test_pack_credential_returns_raw_key():
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    assert OCSStrategy.pack_credential({"api_key": "ocs_xxx"}) == "ocs_xxx"


@pytest.mark.asyncio
async def test_verify_and_discover_single_page(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={
            "results": [
                {"id": "exp-1", "name": "Bot One"},
                {"id": "exp-2", "name": "Bot Two"},
            ],
            "next": None,
        },
        status_code=200,
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert descriptors == [("exp-1", "Bot One"), ("exp-2", "Bot Two")]
    request = httpx_mock.get_request()
    assert request.headers["X-api-key"] == "k"


@pytest.mark.asyncio
async def test_verify_and_discover_paginates(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={
            "results": [{"id": "exp-1", "name": "Bot One"}],
            "next": "https://ocs.example.com/api/experiments/?cursor=xyz",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/?cursor=xyz",
        json={"results": [{"id": "exp-2", "name": "Bot Two"}], "next": None},
    )
    descriptors = await OCSStrategy.verify_and_discover({"api_key": "k"})
    assert [d.external_id for d in descriptors] == ["exp-1", "exp-2"]


@pytest.mark.asyncio
async def test_verify_and_discover_unauthorized(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        status_code=401,
    )
    with pytest.raises(CredentialVerificationError):
        await OCSStrategy.verify_and_discover({"api_key": "bad"})


@pytest.mark.asyncio
async def test_verify_and_discover_empty_list_raises(httpx_mock, settings):
    """A valid key with no experiments cannot be used as a connection."""
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={"results": [], "next": None},
        status_code=200,
    )
    with pytest.raises(CredentialVerificationError, match="no experiments"):
        await OCSStrategy.verify_and_discover({"api_key": "k"})


@pytest.mark.asyncio
async def test_verify_for_tenant_passes_when_experiment_present(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={"results": [{"id": "exp-1", "name": "Bot"}], "next": None},
    )
    await OCSStrategy.verify_for_tenant({"api_key": "k"}, external_id="exp-1")


@pytest.mark.asyncio
async def test_verify_for_tenant_fails_when_experiment_missing(httpx_mock, settings):
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    settings.OCS_URL = "https://ocs.example.com"
    httpx_mock.add_response(
        method="GET",
        url="https://ocs.example.com/api/experiments/",
        json={"results": [{"id": "exp-other", "name": "Other"}], "next": None},
    )
    with pytest.raises(CredentialVerificationError, match="exp-1"):
        await OCSStrategy.verify_for_tenant({"api_key": "k"}, external_id="exp-1")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_key_strategies_ocs.py -v`
Expected: FAIL with `ModuleNotFoundError: apps.users.services.api_key_providers.ocs`.

**Step 3: Implement OCSStrategy**

```python
# apps/users/services/api_key_providers/ocs.py
"""Open Chat Studio API-key strategy."""

from __future__ import annotations

import httpx
from django.conf import settings

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)

OCS_DEFAULT_URL = "https://www.openchatstudio.com"


def _auth_header(api_key: str) -> dict[str, str]:
    return {"X-api-key": api_key}


def _experiments_url() -> str:
    base = getattr(settings, "OCS_URL", OCS_DEFAULT_URL).rstrip("/")
    return f"{base}/api/experiments/"


async def _list_experiments(api_key: str) -> list[dict]:
    """Paginate through /api/experiments/ and return all results.

    Raises CredentialVerificationError on auth failure or unexpected status.
    """
    headers = _auth_header(api_key)
    results: list[dict] = []
    url: str | None = _experiments_url()
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise CredentialVerificationError(
                    f"OCS rejected the API key (HTTP {resp.status_code})"
                )
            if not resp.is_success:
                raise CredentialVerificationError(
                    f"OCS API returned unexpected status {resp.status_code}"
                )
            payload = resp.json()
            results.extend(payload.get("results", []))
            url = payload.get("next")
    return results


class OCSStrategy(CredentialProviderStrategy):
    provider_id = "ocs"
    display_name = "Open Chat Studio"
    form_fields: list[FormField] = [
        {"key": "api_key", "label": "API Key", "type": "password",
         "required": True, "editable_on_rotate": True},
    ]

    @classmethod
    def pack_credential(cls, fields: dict[str, str]) -> str:
        return fields["api_key"]

    @classmethod
    async def verify_and_discover(cls, fields: dict[str, str]) -> list[TenantDescriptor]:
        experiments = await _list_experiments(fields["api_key"])
        if not experiments:
            raise CredentialVerificationError(
                "OCS API key is valid but has no experiments accessible"
            )
        return [
            TenantDescriptor(str(e["id"]), e.get("name") or str(e["id"]))
            for e in experiments
        ]

    @classmethod
    async def verify_for_tenant(cls, fields: dict[str, str], external_id: str) -> None:
        experiments = await _list_experiments(fields["api_key"])
        for e in experiments:
            if str(e["id"]) == external_id:
                return
        raise CredentialVerificationError(
            f"API key does not have access to experiment '{external_id}'"
        )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_key_strategies_ocs.py -v`
Expected: All PASS.

**Step 5: Lint and commit**

```bash
uv run ruff check apps/users/services/api_key_providers/ocs.py tests/test_api_key_strategies_ocs.py
uv run ruff format apps/users/services/api_key_providers/ocs.py tests/test_api_key_strategies_ocs.py
git add apps/users/services/api_key_providers/ocs.py tests/test_api_key_strategies_ocs.py
git commit -m "feat(auth): add OCS API-key provider strategy"
```

---

## Task 4: Strategy registry

**Files:**
- Create: `apps/users/services/api_key_providers/registry.py`
- Modify: `apps/users/services/api_key_providers/__init__.py`
- Create: `tests/test_api_key_provider_registry.py`

**Step 1: Write the failing test**

```python
# tests/test_api_key_provider_registry.py
def test_registry_contains_expected_providers():
    from apps.users.services.api_key_providers.registry import STRATEGIES
    from apps.users.services.api_key_providers.commcare import CommCareStrategy
    from apps.users.services.api_key_providers.ocs import OCSStrategy

    assert STRATEGIES["commcare"] is CommCareStrategy
    assert STRATEGIES["ocs"] is OCSStrategy


def test_get_strategy_returns_class_or_none():
    from apps.users.services.api_key_providers.registry import get_strategy

    cls = get_strategy("ocs")
    assert cls is not None
    assert cls.provider_id == "ocs"
    assert get_strategy("does-not-exist") is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_key_provider_registry.py -v`
Expected: FAIL with `ImportError: cannot import name 'STRATEGIES'`.

**Step 3: Implement the registry**

```python
# apps/users/services/api_key_providers/registry.py
"""Provider-strategy registry."""

from __future__ import annotations

from apps.users.services.api_key_providers.base import CredentialProviderStrategy
from apps.users.services.api_key_providers.commcare import CommCareStrategy
from apps.users.services.api_key_providers.ocs import OCSStrategy

STRATEGIES: dict[str, type[CredentialProviderStrategy]] = {
    CommCareStrategy.provider_id: CommCareStrategy,
    OCSStrategy.provider_id: OCSStrategy,
}


def get_strategy(provider_id: str) -> type[CredentialProviderStrategy] | None:
    return STRATEGIES.get(provider_id)
```

Update the package init to re-export:

```python
# apps/users/services/api_key_providers/__init__.py
"""Provider-strategy abstraction for API-key authentication."""

from apps.users.services.api_key_providers.base import (
    CredentialProviderStrategy,
    CredentialVerificationError,
    FormField,
    TenantDescriptor,
)
from apps.users.services.api_key_providers.registry import STRATEGIES, get_strategy

__all__ = [
    "STRATEGIES",
    "CredentialProviderStrategy",
    "CredentialVerificationError",
    "FormField",
    "TenantDescriptor",
    "get_strategy",
]
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_key_provider_registry.py -v`
Expected: All PASS.

**Step 5: Lint and commit**

```bash
uv run ruff check apps/users/services/api_key_providers/ tests/test_api_key_provider_registry.py
uv run ruff format apps/users/services/api_key_providers/ tests/test_api_key_provider_registry.py
git add apps/users/services/api_key_providers/ tests/test_api_key_provider_registry.py
git commit -m "feat(auth): wire strategy registry"
```

---

## Task 5: GET /api/auth/api-key-providers/ endpoint (TDD)

**Files:**
- Modify: `apps/users/views.py` — add `api_key_providers_view`
- Modify: `apps/users/auth_urls.py` — add URL pattern
- Create: `tests/test_api_key_providers_view.py`

**Step 1: Write the failing test**

```python
# tests/test_api_key_providers_view.py
import pytest
from django.test import Client


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="u@example.com", password="pw")


def test_returns_strategy_metadata(client, user):
    client.force_login(user)
    resp = client.get("/api/auth/api-key-providers/")
    assert resp.status_code == 200
    providers = resp.json()
    by_id = {p["id"]: p for p in providers}
    assert "commcare" in by_id
    assert "ocs" in by_id
    assert by_id["commcare"]["display_name"] == "CommCare HQ"
    assert by_id["ocs"]["display_name"] == "Open Chat Studio"
    ocs_field_keys = [f["key"] for f in by_id["ocs"]["fields"]]
    assert ocs_field_keys == ["api_key"]
    cc_field_keys = [f["key"] for f in by_id["commcare"]["fields"]]
    assert cc_field_keys == ["domain", "username", "api_key"]


def test_unauthenticated_returns_401(client, db):
    resp = client.get("/api/auth/api-key-providers/")
    assert resp.status_code == 401
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api_key_providers_view.py -v`
Expected: FAIL with 404 (URL not registered yet).

**Step 3: Implement the view**

Add to `apps/users/views.py` (top-level, near the other tenant views). Add the import to the existing import block:

```python
from apps.users.services.api_key_providers import STRATEGIES
```

Then the view:

```python
@require_http_methods(["GET"])
@async_login_required
async def api_key_providers_view(request):
    """GET /api/auth/api-key-providers/ — list registered API-key strategies
    so the frontend can render the Add/Edit dialog dynamically."""
    payload = [
        {
            "id": strategy.provider_id,
            "display_name": strategy.display_name,
            "fields": list(strategy.form_fields),
        }
        for strategy in STRATEGIES.values()
    ]
    return JsonResponse(payload, safe=False)
```

Add to `apps/users/auth_urls.py`. Update the import:

```python
from apps.users.views import (
    api_key_providers_view,
    tenant_credential_detail_view,
    tenant_credential_list_view,
    tenant_ensure_view,
    tenant_list_view,
    tenant_select_view,
)
```

Add to `urlpatterns` (after `tenant-credentials/<id>/`):

```python
path("api-key-providers/", api_key_providers_view, name="api-key-providers"),
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_key_providers_view.py -v`
Expected: All PASS.

**Step 5: Lint and commit**

```bash
uv run ruff check apps/users/views.py apps/users/auth_urls.py tests/test_api_key_providers_view.py
uv run ruff format apps/users/views.py apps/users/auth_urls.py tests/test_api_key_providers_view.py
git add apps/users/views.py apps/users/auth_urls.py tests/test_api_key_providers_view.py
git commit -m "feat(auth): add GET /api/auth/api-key-providers/ endpoint"
```

---

## Task 6: Refactor POST /api/auth/tenant-credentials/ (TDD)

This task changes the request shape and the response shape. The CommCare existing tests in `tests/test_users.py` and `tests/test_tenant_api.py` will break — update them to match the new shape as part of this task.

**Files:**
- Modify: `apps/users/views.py` — replace POST branch in `tenant_credential_list_view`
- Modify: `tests/test_users.py` — update existing CommCare POST tests
- Create: `tests/test_tenant_credentials_post.py` — new tests covering OCS POST and edge cases

**Step 1: Write the failing tests for the new shape**

```python
# tests/test_tenant_credentials_post.py
import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.services.api_key_providers import (
    CredentialVerificationError,
    TenantDescriptor,
)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(email="u@example.com", password="pw")


def _post(client, body):
    return client.post(
        "/api/auth/tenant-credentials/",
        data=json.dumps(body),
        content_type="application/json",
    )


def test_commcare_post_returns_single_membership(client, user):
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
        return_value=[TenantDescriptor("dimagi", "dimagi")],
    ):
        resp = _post(
            client,
            {
                "provider": "commcare",
                "fields": {"domain": "dimagi", "username": "u", "api_key": "k"},
            },
        )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert len(body["memberships"]) == 1
    m = body["memberships"][0]
    assert m["tenant_id"] == "dimagi"
    assert m["tenant_name"] == "dimagi"


def test_ocs_post_returns_multiple_memberships(client, user):
    client.force_login(user)
    descriptors = [
        TenantDescriptor("exp-1", "Bot One"),
        TenantDescriptor("exp-2", "Bot Two"),
    ]
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        return_value=descriptors,
    ):
        resp = _post(
            client,
            {"provider": "ocs", "fields": {"api_key": "ocs_xxx"}},
        )
    assert resp.status_code == 201, resp.content
    memberships = resp.json()["memberships"]
    assert {m["tenant_id"] for m in memberships} == {"exp-1", "exp-2"}

    from apps.users.models import Tenant, TenantCredential, TenantMembership
    assert Tenant.objects.filter(provider="ocs").count() == 2
    assert TenantMembership.objects.filter(user=user, tenant__provider="ocs").count() == 2
    # All memberships share the same packed credential
    creds = TenantCredential.objects.filter(tenant_membership__user=user)
    assert {c.encrypted_credential for c in creds}  # non-empty
    assert all(c.credential_type == TenantCredential.API_KEY for c in creds)


def test_unknown_provider_returns_400(client, user):
    client.force_login(user)
    resp = _post(client, {"provider": "fake", "fields": {}})
    assert resp.status_code == 400
    assert "fake" in resp.json()["error"].lower()


def test_missing_required_field_returns_400(client, user):
    client.force_login(user)
    resp = _post(client, {"provider": "ocs", "fields": {}})
    assert resp.status_code == 400
    assert "api_key" in resp.json()["error"]


def test_verification_error_returns_400(client, user):
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        side_effect=CredentialVerificationError("nope"),
    ):
        resp = _post(client, {"provider": "ocs", "fields": {"api_key": "bad"}})
    assert resp.status_code == 400
    assert "nope" in resp.json()["error"]


def test_partial_failure_is_atomic(client, user):
    """If membership creation fails partway, no rows are persisted."""
    client.force_login(user)
    descriptors = [
        TenantDescriptor("exp-1", "Bot One"),
        TenantDescriptor("exp-2", "Bot Two"),
    ]
    # Force encrypt_credential to fail on the second iteration.
    real_encrypt = None

    def flaky_encrypt(value):
        # Allow first call, fail subsequent calls.
        if not hasattr(flaky_encrypt, "calls"):
            flaky_encrypt.calls = 0
        flaky_encrypt.calls += 1
        if flaky_encrypt.calls > 1:
            raise ValueError("boom")
        return real_encrypt(value)

    from apps.users.adapters import encrypt_credential
    real_encrypt = encrypt_credential
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
        return_value=descriptors,
    ), patch("apps.users.views.encrypt_credential", side_effect=flaky_encrypt):
        resp = _post(client, {"provider": "ocs", "fields": {"api_key": "k"}})
    assert resp.status_code == 500
    from apps.users.models import TenantMembership
    assert not TenantMembership.objects.filter(user=user).exists()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tenant_credentials_post.py -v`
Expected: All FAIL — endpoint still uses old shape.

**Step 3: Update the existing CommCare POST tests in tests/test_users.py**

Edit `tests/test_users.py` `test_post_creates_membership_and_credential` (around lines 100-126) — change the request body and the response assertion:

```python
def test_post_creates_membership_and_credential(self, client, db, user):
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover",
        return_value=[
            __import__("apps.users.services.api_key_providers", fromlist=["TenantDescriptor"]).TenantDescriptor(
                "my-domain", "my-domain"
            )
        ],
    ):
        resp = client.post(
            "/api/auth/tenant-credentials/",
            data={
                "provider": "commcare",
                "fields": {
                    "domain": "my-domain",
                    "username": "user@example.com",
                    "api_key": "abc123",
                },
            },
            content_type="application/json",
        )
    assert resp.status_code == 201
    body = resp.json()
    assert "memberships" in body
    membership = body["memberships"][0]
    membership_id = membership["membership_id"]

    from apps.users.models import TenantCredential, TenantMembership
    tm = TenantMembership.objects.get(id=membership_id)
    assert tm.tenant.provider == "commcare"
    assert tm.tenant.external_id == "my-domain"
    cred = TenantCredential.objects.get(tenant_membership=tm)
    assert cred.credential_type == TenantCredential.API_KEY
```

Apply the same body shape change to `test_api_key_stored_encrypted` (lines 128-153):
- Replace the `data=` with `{"provider": "commcare", "fields": {"domain": "secure-domain", "username": "user@example.com", "api_key": "supersecretkey"}}`.
- Update the patch target from `apps.users.views.verify_commcare_credential` to `apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_and_discover` with the descriptor return value.
- Update the assertion: `plaintext = "user@example.com:supersecretkey"` is what the strategy packs, so the `decrypt_credential(...) == plaintext` assertion still holds.

**Step 4: Implement the new POST handler**

Replace the POST branch in `apps/users/views.py` `tenant_credential_list_view` (current lines 152-209) with:

```python
# POST — create API-key-backed membership(s) via strategy registry
try:
    body = json.loads(request.body)
except (json.JSONDecodeError, ValueError):
    return JsonResponse({"error": "Invalid JSON"}, status=400)

provider = body.get("provider", "").strip()
fields = body.get("fields") or {}

strategy = STRATEGIES.get(provider)
if strategy is None:
    return JsonResponse(
        {"error": f"Unknown provider '{provider}'"}, status=400
    )

missing = [
    f["key"] for f in strategy.form_fields
    if f["required"] and not (fields.get(f["key"]) or "").strip()
]
if missing:
    return JsonResponse(
        {"error": f"Missing required field(s): {', '.join(missing)}"},
        status=400,
    )

try:
    descriptors = await strategy.verify_and_discover(fields)
except CredentialVerificationError as e:
    return JsonResponse({"error": str(e)}, status=400)

try:
    packed = strategy.pack_credential(fields)
    encrypted = encrypt_credential(packed)
except ValueError as e:
    return JsonResponse({"error": str(e)}, status=500)

memberships_payload = []
try:
    async with atransaction():
        for desc in descriptors:
            tenant, _ = await Tenant.objects.aget_or_create(
                provider=provider,
                external_id=desc.external_id,
                defaults={"canonical_name": desc.canonical_name},
            )
            tm, _ = await TenantMembership.objects.aget_or_create(
                user=user, tenant=tenant
            )
            await TenantCredential.objects.aupdate_or_create(
                tenant_membership=tm,
                defaults={
                    "credential_type": TenantCredential.API_KEY,
                    "encrypted_credential": encrypted,
                },
            )
            memberships_payload.append(
                {
                    "membership_id": str(tm.id),
                    "tenant_id": tenant.external_id,
                    "tenant_name": tenant.canonical_name,
                }
            )
except Exception as e:
    logger.exception("Failed to persist memberships for provider %s", provider)
    return JsonResponse({"error": str(e)}, status=500)

return JsonResponse({"memberships": memberships_payload}, status=201)
```

Add these imports near the top of `apps/users/views.py`:

```python
from apps.users.services.api_key_providers import (
    STRATEGIES,
    CredentialVerificationError,
)
```

For the atomic transaction context, use Django's `transaction.atomic()` wrapped in `asgiref.sync.sync_to_async` is awkward — Django 5 ships `from django.db.transaction import atransaction` only in newer versions. **Verify which is available:**

Run: `uv run python -c "from django.db.transaction import atransaction; print('ok')" 2>&1 | head -3`

If it fails, replace `async with atransaction():` with the `transaction.atomic()` pattern below by inlining the loop into a sync helper:

```python
from asgiref.sync import sync_to_async
from django.db import transaction


@sync_to_async
def _persist_memberships(user, provider, descriptors, encrypted):
    rows = []
    with transaction.atomic():
        for desc in descriptors:
            tenant, _ = Tenant.objects.get_or_create(
                provider=provider,
                external_id=desc.external_id,
                defaults={"canonical_name": desc.canonical_name},
            )
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
            TenantCredential.objects.update_or_create(
                tenant_membership=tm,
                defaults={
                    "credential_type": TenantCredential.API_KEY,
                    "encrypted_credential": encrypted,
                },
            )
            rows.append(
                {
                    "membership_id": str(tm.id),
                    "tenant_id": tenant.external_id,
                    "tenant_name": tenant.canonical_name,
                }
            )
    return rows
```

The CLAUDE.md note "Do not use `sync_to_async` for ORM calls" applies to ad-hoc reads in async views; using it to wrap a transactional write block is the correct workaround until Django ships true async transactions. Add a one-line comment in the code explaining this.

Then in the view: `memberships_payload = await _persist_memberships(user, provider, descriptors, encrypted)`.

The unused `from apps.users.services.tenant_verification import …` import in `views.py` becomes dead — remove it (Task 6 also has to delete the now-unused `verify_commcare_credential` import unless PATCH still uses it). Defer cleanup to Task 7 once both endpoints have migrated.

**Step 5: Run all affected tests**

Run: `uv run pytest tests/test_tenant_credentials_post.py tests/test_users.py::TestTenantCredentialEndpoints -v`
Expected: All PASS.

**Step 6: Lint and commit**

```bash
uv run ruff check apps/users/views.py tests/test_tenant_credentials_post.py tests/test_users.py
uv run ruff format apps/users/views.py tests/test_tenant_credentials_post.py tests/test_users.py
git add apps/users/views.py tests/test_tenant_credentials_post.py tests/test_users.py
git commit -m "feat(auth): refactor POST /tenant-credentials/ to dispatch via strategy registry"
```

---

## Task 7: Refactor PATCH /api/auth/tenant-credentials/<id>/ (TDD)

This task changes the PATCH request shape. Existing PATCH tests in `tests/test_tenant_api.py` will break — update them.

**Files:**
- Modify: `apps/users/views.py` — replace PATCH branch in `tenant_credential_detail_view`
- Modify: `tests/test_tenant_api.py` — update existing PATCH tests
- Create: `tests/test_tenant_credentials_patch.py` — OCS PATCH coverage

**Step 1: Write the failing tests**

```python
# tests/test_tenant_credentials_patch.py
import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(email="u@example.com", password="pw")


def _make_ocs_membership(user):
    from apps.users.adapters import encrypt_credential
    from apps.users.models import Tenant, TenantCredential, TenantMembership

    tenant = Tenant.objects.create(
        provider="ocs", external_id="exp-1", canonical_name="Bot One"
    )
    tm = TenantMembership.objects.create(user=user, tenant=tenant)
    TenantCredential.objects.create(
        tenant_membership=tm,
        credential_type=TenantCredential.API_KEY,
        encrypted_credential=encrypt_credential("old_ocs_key"),
    )
    return tm


def test_patch_ocs_rotates_key(user):
    tm = _make_ocs_membership(user)
    client = Client()
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_for_tenant",
        return_value=None,
    ):
        resp = client.patch(
            f"/api/auth/tenant-credentials/{tm.id}/",
            data=json.dumps({"fields": {"api_key": "new_ocs_key"}}),
            content_type="application/json",
        )
    assert resp.status_code == 200
    from apps.users.adapters import decrypt_credential
    tm.credential.refresh_from_db()
    assert decrypt_credential(tm.credential.encrypted_credential) == "new_ocs_key"


def test_patch_ocs_rejects_invalid_key(user):
    from apps.users.services.api_key_providers import CredentialVerificationError

    tm = _make_ocs_membership(user)
    client = Client()
    client.force_login(user)
    with patch(
        "apps.users.services.api_key_providers.ocs.OCSStrategy.verify_for_tenant",
        side_effect=CredentialVerificationError("revoked"),
    ):
        resp = client.patch(
            f"/api/auth/tenant-credentials/{tm.id}/",
            data=json.dumps({"fields": {"api_key": "bad"}}),
            content_type="application/json",
        )
    assert resp.status_code == 400


def test_patch_missing_required_editable_field_returns_400(user):
    tm = _make_ocs_membership(user)
    client = Client()
    client.force_login(user)
    resp = client.patch(
        f"/api/auth/tenant-credentials/{tm.id}/",
        data=json.dumps({"fields": {}}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "api_key" in resp.json()["error"]
```

**Step 2: Update existing CommCare PATCH tests**

In `tests/test_tenant_api.py`, find the patch tests around line 80-160. The body shape changes from `{"credential": "user:key"}` to `{"fields": {"username": "user", "api_key": "key"}}`. Update the patch targets:

- `apps.users.views.verify_commcare_credential` → `apps.users.services.api_key_providers.commcare.CommCareStrategy.verify_for_tenant`
- The mocked side_effect for the failure case: `side_effect=CommCareVerificationError(...)` → `side_effect=CredentialVerificationError(...)` (and update the import).
- Successful return values can be `return_value=None` (verify_for_tenant returns None on success).

**Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_tenant_credentials_patch.py tests/test_tenant_api.py -v -k patch`
Expected: All FAIL — endpoint still uses old shape.

**Step 4: Implement the new PATCH handler**

Replace the PATCH branch in `apps/users/views.py` `tenant_credential_detail_view` (current lines 227-270):

```python
# PATCH — rotate API key via strategy registry
try:
    body = json.loads(request.body)
except (json.JSONDecodeError, ValueError):
    return JsonResponse({"error": "Invalid JSON"}, status=400)

fields = body.get("fields") or {}

try:
    tm = await TenantMembership.objects.select_related("credential", "tenant").aget(
        id=membership_id, user=user
    )
except TenantMembership.DoesNotExist:
    return JsonResponse({"error": "Not found"}, status=404)
if not hasattr(tm, "credential"):
    return JsonResponse({"error": "Not found"}, status=404)

strategy = STRATEGIES.get(tm.tenant.provider)
if strategy is None:
    return JsonResponse(
        {"error": f"Provider '{tm.tenant.provider}' has no API-key strategy"},
        status=400,
    )

editable = [f for f in strategy.form_fields if f["editable_on_rotate"]]
missing = [f["key"] for f in editable if f["required"] and not (fields.get(f["key"]) or "").strip()]
if missing:
    return JsonResponse(
        {"error": f"Missing required field(s): {', '.join(missing)}"},
        status=400,
    )

# Merge editable fields with the existing tenant external_id for verification.
# Strategies whose pack_credential needs a non-editable field (CommCare's
# domain) must accept the missing key — CommCareStrategy.verify_for_tenant
# uses external_id directly, so this works.
try:
    await strategy.verify_for_tenant(fields, external_id=tm.tenant.external_id)
except CredentialVerificationError as e:
    return JsonResponse({"error": str(e)}, status=400)

# Pack: for CommCare we need to construct a fields dict containing the
# original domain (so pack_credential's signature is satisfied). We pass
# the editable fields as-is plus the existing external_id under whichever
# non-editable key the strategy expects. For both current strategies, the
# editable field set is sufficient for pack_credential; CommCare doesn't
# pack `domain`, only username:api_key.
try:
    packed = strategy.pack_credential(fields)
    encrypted = encrypt_credential(packed)
except (KeyError, ValueError) as e:
    return JsonResponse({"error": str(e)}, status=400)

tm.credential.encrypted_credential = encrypted
await tm.credential.asave(update_fields=["encrypted_credential"])
return JsonResponse(
    {
        "membership_id": str(tm.id),
        "tenant_id": tm.tenant.external_id,
        "tenant_name": tm.tenant.canonical_name,
    }
)
```

Now remove the dead imports at the top of `apps/users/views.py`:

```python
# Remove these lines if no other code in views.py references them:
from apps.users.services.tenant_verification import (
    CommCareVerificationError,
    verify_commcare_credential,
)
```

**Step 5: Run all affected tests**

Run: `uv run pytest tests/test_tenant_credentials_patch.py tests/test_tenant_api.py -v`
Expected: All PASS.

**Step 6: Run the full users test suite as a regression check**

Run: `uv run pytest tests/test_users.py tests/test_tenant_api.py tests/test_tenant_credentials_post.py tests/test_tenant_credentials_patch.py tests/test_api_key_providers_view.py tests/test_api_key_strategies_commcare.py tests/test_api_key_strategies_ocs.py tests/test_api_key_provider_registry.py -v`
Expected: All PASS.

**Step 7: Lint and commit**

```bash
uv run ruff check apps/users/views.py tests/test_tenant_credentials_patch.py tests/test_tenant_api.py
uv run ruff format apps/users/views.py tests/test_tenant_credentials_patch.py tests/test_tenant_api.py
git add apps/users/views.py tests/test_tenant_credentials_patch.py tests/test_tenant_api.py
git commit -m "feat(auth): refactor PATCH /tenant-credentials/ to dispatch via strategy registry"
```

---

## Task 8: OCSBaseLoader auth-header dispatch (TDD)

**Files:**
- Modify: `mcp_server/loaders/ocs_base.py`
- Create: `tests/test_ocs_base_loader_auth.py`

**Step 1: Write the failing test**

```python
# tests/test_ocs_base_loader_auth.py
def test_oauth_credential_uses_bearer_header():
    from mcp_server.loaders.ocs_base import OCSBaseLoader

    loader = OCSBaseLoader(
        experiment_id="exp-1",
        credential={"type": "oauth", "value": "tok123"},
    )
    assert loader._session.headers["Authorization"] == "Bearer tok123"


def test_api_key_credential_uses_x_api_key_header():
    from mcp_server.loaders.ocs_base import OCSBaseLoader

    loader = OCSBaseLoader(
        experiment_id="exp-1",
        credential={"type": "api_key", "value": "ocs_xxx"},
    )
    assert loader._session.headers["X-api-key"] == "ocs_xxx"
    assert "Authorization" not in loader._session.headers


def test_default_credential_type_treated_as_oauth():
    """Backward compat: missing 'type' key defaults to OAuth/Bearer."""
    from mcp_server.loaders.ocs_base import OCSBaseLoader

    loader = OCSBaseLoader(
        experiment_id="exp-1",
        credential={"value": "tok"},
    )
    assert loader._session.headers["Authorization"] == "Bearer tok"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ocs_base_loader_auth.py -v`
Expected: `test_api_key_credential_uses_x_api_key_header` FAILS (header is `Authorization: Bearer`).

**Step 3: Update OCSBaseLoader.__init__**

In `mcp_server/loaders/ocs_base.py`, replace the auth header line in `__init__`:

```python
# Replace:
self._session.headers.update({"Authorization": f"Bearer {credential['value']}"})
# With:
if credential.get("type") == "api_key":
    self._session.headers.update({"X-api-key": credential["value"]})
else:
    self._session.headers.update({"Authorization": f"Bearer {credential['value']}"})
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ocs_base_loader_auth.py -v`
Expected: All PASS.

**Step 5: Run the existing OCS loader tests to confirm no regression**

Run: `uv run pytest tests/ -v -k "ocs"`
Expected: All PASS.

**Step 6: Lint and commit**

```bash
uv run ruff check mcp_server/loaders/ocs_base.py tests/test_ocs_base_loader_auth.py
uv run ruff format mcp_server/loaders/ocs_base.py tests/test_ocs_base_loader_auth.py
git add mcp_server/loaders/ocs_base.py tests/test_ocs_base_loader_auth.py
git commit -m "feat(loaders): dispatch OCS loader auth header on credential type"
```

---

## Task 9: Frontend ApiConnectionDialog component

**Files:**
- Create: `frontend/src/components/ApiConnectionDialog/ApiConnectionDialog.tsx`
- Create: `frontend/src/components/ApiConnectionDialog/index.ts`

**Step 1: Create the component**

```tsx
// frontend/src/components/ApiConnectionDialog/ApiConnectionDialog.tsx
import { useState, useEffect, type FormEvent } from "react"
import { api } from "@/api/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Button } from "@/components/ui/button"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"

export interface ApiKeyConnection {
  membership_id: string
  provider: string
  tenant_id: string
  tenant_name: string
  credential_type: string
}

interface ProviderField {
  key: string
  label: string
  type: "text" | "password"
  required: boolean
  editable_on_rotate: boolean
}

interface ProviderSchema {
  id: string
  display_name: string
  fields: ProviderField[]
}

interface MembershipResult {
  membership_id: string
  tenant_id: string
  tenant_name: string
}

interface Props {
  open: boolean
  mode: "add" | "edit"
  editing: ApiKeyConnection | null
  onClose: () => void
  onSaved: () => void | Promise<void>
}

export function ApiConnectionDialog({ open, mode, editing, onClose, onSaved }: Props) {
  const [schemas, setSchemas] = useState<ProviderSchema[]>([])
  const [providerId, setProviderId] = useState<string>("")
  const [values, setValues] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    api
      .get<ProviderSchema[]>("/api/auth/api-key-providers/")
      .then((data) => {
        setSchemas(data)
        // Edit mode: lock to the row's provider. Add mode: pick first.
        const initial = mode === "edit" && editing ? editing.provider : data[0]?.id ?? ""
        setProviderId(initial)
        setValues({})
        setError(null)
      })
      .catch(() => setError("Failed to load provider list."))
  }, [open, mode, editing])

  const schema = schemas.find((s) => s.id === providerId) ?? null
  const visibleFields = schema?.fields.filter((f) =>
    mode === "edit" ? f.editable_on_rotate : true
  ) ?? []

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!schema) return
    setLoading(true)
    setError(null)
    try {
      if (mode === "edit" && editing) {
        await api.patch(
          `/api/auth/tenant-credentials/${editing.membership_id}/`,
          { fields: values }
        )
      } else {
        await api.post<{ memberships: MembershipResult[] }>(
          "/api/auth/tenant-credentials/",
          { provider: providerId, fields: values }
        )
      }
      await onSaved()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save connection.")
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? null : onClose())}>
      <DialogContent data-testid="api-connection-dialog">
        <DialogHeader>
          <DialogTitle>
            {mode === "edit" ? "Edit API connection" : "Add API connection"}
          </DialogTitle>
          <DialogDescription>
            {mode === "edit"
              ? "Rotate the API key for this connection."
              : "Connect a provider with a personal API key."}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {mode === "add" && schemas.length > 1 && (
            <div className="space-y-2">
              <Label>Provider</Label>
              <RadioGroup value={providerId} onValueChange={setProviderId}>
                {schemas.map((s) => (
                  <div key={s.id} className="flex items-center gap-2">
                    <RadioGroupItem
                      value={s.id}
                      id={`provider-${s.id}`}
                      data-testid={`api-connection-provider-${s.id}`}
                    />
                    <Label htmlFor={`provider-${s.id}`}>{s.display_name}</Label>
                  </div>
                ))}
              </RadioGroup>
            </div>
          )}

          {visibleFields.map((f) => (
            <div key={f.key} className="space-y-2">
              <Label htmlFor={`field-${f.key}`}>{f.label}</Label>
              <Input
                id={`field-${f.key}`}
                type={f.type}
                required={f.required}
                value={values[f.key] ?? ""}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
                data-testid={`api-connection-field-${f.key}`}
              />
            </div>
          ))}

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={loading || !schema}
              data-testid="api-connection-submit"
            >
              {loading ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
```

```ts
// frontend/src/components/ApiConnectionDialog/index.ts
export { ApiConnectionDialog, type ApiKeyConnection } from "./ApiConnectionDialog"
```

**Step 2: Verify it compiles**

Run: `cd frontend && bun run build 2>&1 | tail -20`
Expected: Build succeeds. (If it fails on missing `RadioGroup`, check `frontend/src/components/ui/` and use `@/components/ui/radio-group`. If radio-group is not yet imported, install it from shadcn or substitute a `<select>` element. Verify with `ls frontend/src/components/ui/radio-group.tsx` before building.)

**Step 3: Lint and commit**

```bash
cd frontend && bun run lint && cd ..
git add frontend/src/components/ApiConnectionDialog/
git commit -m "feat(frontend): add schema-driven ApiConnectionDialog"
```

---

## Task 10: Refactor ConnectionsPage to use the dialog

**Files:**
- Modify: `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx`

**Step 1: Update the page**

The full file is ~600 lines; this is a structural refactor. Make these changes:

1. Replace `ApiKeyDomain` → `ApiKeyConnection` (also import from the new dialog component).
2. Replace `domains` state → `connections` (semantic rename).
3. Delete the inline form-state hooks: `formDomain`, `formUsername`, `formApiKey`, `formLoading`, `formError`, `editingDomain` (the dialog manages its own state).
4. Delete `openAddDialog`, `openEditDialog`, `closeDialog`, `handleSubmit` — replaced by passing `mode` + `editing` to the dialog component.
5. Add `dialogState`:
   ```tsx
   const [dialogState, setDialogState] = useState<
     { mode: "add" } | { mode: "edit"; editing: ApiKeyConnection } | null
   >(null)
   ```
6. The "Add API Connection" button:
   ```tsx
   <Button onClick={() => setDialogState({ mode: "add" })}>
     Add API Connection
   </Button>
   ```
7. Edit row action:
   ```tsx
   onClick={() => setDialogState({ mode: "edit", editing: row })}
   ```
8. Render the dialog:
   ```tsx
   <ApiConnectionDialog
     open={dialogState !== null}
     mode={dialogState?.mode ?? "add"}
     editing={dialogState?.mode === "edit" ? dialogState.editing : null}
     onClose={() => setDialogState(null)}
     onSaved={async () => {
       await fetchConnections()
       void fetchStoreDomains()
     }}
   />
   ```
9. Section heading: rename "Connected Domains" → "API Key Connections".
10. Delete the (now unused) `DomainDialog` component at the bottom of the file (lines ~440-560 in the current file — the `DomainDialog` function definition).
11. Update the response handling for `POST` — the response is now `{memberships: [...]}` rather than `{membership_id}`; this is handled inside the dialog now, so the page just refreshes on `onSaved`.

**Step 2: Verify the page still renders**

Run: `cd frontend && bun run build 2>&1 | tail -20`
Expected: Build succeeds.

**Step 3: Lint and commit**

```bash
cd frontend && bun run lint && cd ..
git add frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx
git commit -m "refactor(frontend): use ApiConnectionDialog on ConnectionsPage"
```

---

## Task 11: Manual verification with playwright-cli

REQUIRED SUB-SKILL: @superpowers:verification-before-completion — confirm each item by observing it before claiming done.

**Step 1: Start dev servers**

Run in one terminal: `docker compose up platform-db redis mcp-server`
Run in another: `uv run honcho -f Procfile.dev start`

Wait until Vite reports `Local: http://localhost:5173`.

**Step 2: Initialize playwright**

Run: `bunx playwright-cli install chromium`
(Skip if `.playwright/cli.config.json` already exists.)

**Step 3: Verification walkthrough**

Run each step and confirm visually with `playwright-cli screenshot` between actions.

```bash
playwright-cli open http://localhost:5173/connections
playwright-cli snapshot   # capture refs
# Log in if needed via the dev login flow
```

For each item below, perform the action and screenshot the result:

- [ ] **Add OCS connection:**
  - Click "Add API Connection" → dialog opens with provider radio
  - Select "Open Chat Studio" → only API Key field visible
  - Paste a real OCS API key (or skip if no creds available — note in commit)
  - Click Save → dialog closes
  - Multiple OCS rows appear, one per experiment the key has access to
- [ ] **Edit OCS connection:**
  - Click edit on an OCS row → dialog opens, provider locked to OCS
  - Only API Key field visible (not experiment ID)
  - Paste a new key → Save → row remains
- [ ] **Existing CommCare add:**
  - Click "Add API Connection" → select CommCare → all three fields appear
  - Submit valid CommCare credentials → single row added
- [ ] **Existing CommCare edit:**
  - Click edit on CommCare row → dialog shows username + api_key (no domain)
  - Update → row stays, no errors
- [ ] **Delete OCS connection:** row disappears, no orphan side effects
- [ ] **Console clean:** `playwright-cli console` shows no error-level messages
  during normal interaction

**Step 4: Document the verification**

If all items pass, commit a verification note:

```bash
git commit --allow-empty -m "test: manual playwright verification of api-key dialog (all checklist items pass)"
```

If any item fails, do NOT mark this task complete. Diagnose using @superpowers:systematic-debugging and add a follow-up task.

---

## Task 12: Final regression + push

**Step 1: Run full backend test suite**

Run: `uv run pytest -x`
Expected: All PASS (or only known failures unrelated to this work).

**Step 2: Run lints**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: Clean.

Run: `cd frontend && bun run lint && cd ..`
Expected: Clean.

**Step 3: Push the branch**

Run: `git push -u origin bdr/add-ocs-api-auth`

**Step 4: Open PR**

Use `dev-utils:create-pr` skill or `gh pr create` with title:

> feat(auth): generalize API-key auth to support OCS via strategy registry

Body should reference the design doc and summarize the change set.

---

## Summary of file changes

**New backend files:**
- `apps/users/services/api_key_providers/__init__.py`
- `apps/users/services/api_key_providers/base.py`
- `apps/users/services/api_key_providers/commcare.py`
- `apps/users/services/api_key_providers/ocs.py`
- `apps/users/services/api_key_providers/registry.py`

**New backend tests:**
- `tests/test_api_key_strategies_commcare.py`
- `tests/test_api_key_strategies_ocs.py`
- `tests/test_api_key_provider_registry.py`
- `tests/test_api_key_providers_view.py`
- `tests/test_tenant_credentials_post.py`
- `tests/test_tenant_credentials_patch.py`
- `tests/test_ocs_base_loader_auth.py`

**Modified backend:**
- `apps/users/views.py` (POST/PATCH refactored, new view added, dead imports removed)
- `apps/users/auth_urls.py` (one new URL)
- `mcp_server/loaders/ocs_base.py` (header dispatch)
- `tests/test_users.py`, `tests/test_tenant_api.py` (request shape updates)

**New frontend files:**
- `frontend/src/components/ApiConnectionDialog/ApiConnectionDialog.tsx`
- `frontend/src/components/ApiConnectionDialog/index.ts`

**Modified frontend:**
- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx`

**Untouched (intentionally):**
- `apps/users/services/tenant_verification.py` — `verify_commcare_credential` is no longer called by views, but other code paths (tenant onboarding, signals) may still use it. Verify with `grep -rn verify_commcare_credential apps/ tests/` after Task 7; if unused, remove in Task 12 as a cleanup commit. If used, leave alone.
- `apps/users/services/tenant_resolution.py` — used by the OAuth flow, not the API-key path.
- `OnboardingWizard.tsx` — explicitly out of scope per the design doc.
