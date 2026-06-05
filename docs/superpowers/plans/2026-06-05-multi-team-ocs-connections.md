# Multi-team OCS connections — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an OCS/CommCare credential ("connection") a first-class entity with per-chatbot team identity, so materialization always uses the correct team's credential and never silently uses the wrong one.

**Architecture:** A new `TenantConnection` model (credential only) replaces `TenantCredential`. Each `TenantMembership` (chatbot/domain/opp) gains a `connection` FK, its own `team_slug`/`team_name`, and an `archived_at` soft-delete marker. Resolution selects a chatbot's connection and, for OAuth, fails closed when the live OCS OIDC `team` claim differs from the chatbot's team. One OAuth connection per `(user, provider)`; unlimited API keys.

**Tech Stack:** Django 5 async ORM, allauth `SocialToken`/`SocialAccount`, Fernet (`apps/users/adapters.py`), httpx, React 19 + Vite frontend, pytest + pytest-asyncio.

**Source of truth:** `docs/superpowers/specs/2026-06-05-multi-team-ocs-connections-design.md`. Do not consult other branches. Every OCS API call must be one of `/api/experiments/`, `/api/sessions/`, `/o/userinfo/`, `/o/token/`.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `apps/users/models.py` | `TenantConnection`; `TenantMembership.connection/team_slug/team_name/archived_at`; delete `TenantCredential` | Modify |
| `apps/users/migrations/0006_tenant_connections.py` | schema: add `TenantConnection` + membership fields | Create |
| `apps/users/migrations/0007_migrate_credentials_to_connections.py` | data: copy creds → connections, set `membership.connection` | Create |
| `apps/users/migrations/0008_delete_tenantcredential.py` | drop `TenantCredential` table | Create |
| `apps/users/services/credential_resolver.py` | resolve via `membership.connection` + stale-OAuth guard | Rewrite |
| `apps/users/services/ocs_team.py` | `detect_ocs_team_*` helpers (sessions/userinfo) | Create |
| `apps/users/services/tenant_resolution.py` | OAuth import → connection + per-chatbot team + un-archive | Modify |
| `apps/users/services/api_key_providers/ocs.py` | optional `team_name` form field | Modify |
| `apps/users/views.py` | connection list/create/rotate/remove; API-key add; exclude archived | Modify |
| `apps/users/auth_views.py` | `onboarding_complete` via connection; disconnect archives+deletes OAuth connection | Modify |
| `apps/users/auth_urls.py` | `connections/` routes | Modify |
| `apps/workspaces/tasks.py` | `select_related("connection")`; exclude archived | Modify |
| `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` | connection-grouped UI | Modify |
| `frontend/src/components/ApiConnectionDialog/ApiConnectionDialog.tsx` | optional team-name field | Modify |
| `tests/test_ocs_connections.py` | resolution, import, archive, auto-detect, migration | Create |

---

## Task 1: `TenantConnection` model + `TenantMembership` fields (schema only)

**Files:**
- Modify: `apps/users/models.py`
- Create: `apps/users/migrations/0006_tenant_connections.py` (via makemigrations)
- Test: `tests/test_ocs_connections.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ocs_connections.py
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.users.models import Tenant, TenantConnection, TenantMembership


def _tenant(ext="exp-1"):
    return Tenant.objects.create(provider="ocs", external_id=ext, canonical_name=ext)


@pytest.mark.django_db
def test_connection_is_credential_only_and_links_memberships(user):
    conn = TenantConnection.objects.create(
        user=user, provider="ocs", credential_type=TenantConnection.API_KEY,
        encrypted_credential="enc",
    )
    tm = TenantMembership.objects.create(
        user=user, tenant=_tenant(), connection=conn, team_slug="acme", team_name="Acme",
    )
    assert tm.connection_id == conn.id
    assert list(conn.memberships.all()) == [tm]
    assert tm.archived_at is None


@pytest.mark.django_db
def test_one_oauth_connection_per_user_provider(user):
    TenantConnection.objects.create(user=user, provider="ocs", credential_type=TenantConnection.OAUTH)
    with pytest.raises(IntegrityError):
        TenantConnection.objects.create(user=user, provider="ocs", credential_type=TenantConnection.OAUTH)


@pytest.mark.django_db
def test_multiple_api_key_connections_allowed(user):
    TenantConnection.objects.create(user=user, provider="ocs", credential_type=TenantConnection.API_KEY, encrypted_credential="a")
    TenantConnection.objects.create(user=user, provider="ocs", credential_type=TenantConnection.API_KEY, encrypted_credential="b")
    assert TenantConnection.objects.filter(user=user, provider="ocs").count() == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ocs_connections.py -q`
Expected: FAIL — `ImportError: cannot import name 'TenantConnection'`.

- [ ] **Step 3: Add the model and membership fields**

In `apps/users/models.py`, add after the `Tenant` class import of `uuid`/`settings` (already present). Add `TenantConnection` **above** `TenantMembership` (so the FK string ref resolves either way), and add the four fields to `TenantMembership`. Replace the existing `TenantCredential` later (Task 7) — leave it for now.

```python
class TenantConnection(models.Model):
    """A single credential a user added: one OAuth login or one API key.

    A connection is a credential only. The team a chatbot belongs to is recorded
    on TenantMembership (team_slug/team_name): in v1 a user has at most one OAuth
    connection per provider, and its team can change when they re-authorize.
    """

    OAUTH = "oauth"
    API_KEY = "api_key"
    TYPE_CHOICES = [(OAUTH, "OAuth Token"), (API_KEY, "API Key")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenant_connections"
    )
    provider = models.CharField(max_length=50, choices=PROVIDER_CHOICES)
    credential_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    encrypted_credential = models.CharField(
        max_length=2000, blank=True,
        help_text="Fernet-encrypted opaque string. Empty for OAuth (token lives in allauth).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"],
                condition=models.Q(credential_type="oauth"),
                name="unique_oauth_connection_per_user_provider",
            ),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.provider}:{self.credential_type}"
```

Add to `TenantMembership` (after `last_selected_at`):

```python
    connection = models.ForeignKey(
        "TenantConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
    )
    team_slug = models.CharField(max_length=255, blank=True, default="")
    team_name = models.CharField(max_length=255, blank=True, default="")
    archived_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations users -n tenant_connections`
Expected: creates `apps/users/migrations/0006_tenant_connections.py` adding the model, the 4 fields, and the constraint. Open it and confirm no unexpected operations (it must NOT touch `TenantCredential`).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_ocs_connections.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/users/models.py apps/users/migrations/0006_tenant_connections.py tests/test_ocs_connections.py
git commit -m "feat(users): add TenantConnection + per-chatbot team fields on TenantMembership"
```

---

## Task 2: Data migration — credentials → connections

**Files:**
- Create: `apps/users/migrations/0007_migrate_credentials_to_connections.py`
- Test: `tests/test_ocs_connections.py` (migration test)

- [ ] **Step 1: Write the data migration**

Create `apps/users/migrations/0007_migrate_credentials_to_connections.py`:

```python
from django.db import migrations


def forward(apps, schema_editor):
    TenantCredential = apps.get_model("users", "TenantCredential")
    TenantConnection = apps.get_model("users", "TenantConnection")
    # OAuth: one connection per (user, provider). API key: one per credential row.
    oauth_cache = {}  # (user_id, provider) -> connection
    for cred in TenantCredential.objects.select_related(
        "tenant_membership", "tenant_membership__tenant", "tenant_membership__user"
    ).all():
        tm = cred.tenant_membership
        provider = tm.tenant.provider
        if cred.credential_type == "oauth":
            key = (tm.user_id, provider)
            conn = oauth_cache.get(key)
            if conn is None:
                conn = TenantConnection.objects.create(
                    user=tm.user, provider=provider, credential_type="oauth",
                    encrypted_credential="",
                )
                oauth_cache[key] = conn
        else:
            conn = TenantConnection.objects.create(
                user=tm.user, provider=provider, credential_type="api_key",
                encrypted_credential=cred.encrypted_credential,
            )
        tm.connection = conn
        # legacy team unknown -> leave team_slug/team_name "" (guard skipped)
        tm.save(update_fields=["connection"])


def reverse(apps, schema_editor):
    TenantCredential = apps.get_model("users", "TenantCredential")
    TenantMembership = apps.get_model("users", "TenantMembership")
    for tm in TenantMembership.objects.select_related("connection").all():
        conn = tm.connection
        if conn is None:
            continue
        TenantCredential.objects.update_or_create(
            tenant_membership=tm,
            defaults={
                "credential_type": conn.credential_type,
                "encrypted_credential": conn.encrypted_credential,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("users", "0006_tenant_connections")]
    operations = [migrations.RunPython(forward, reverse)]
```

- [ ] **Step 2: Write the migration test**

```python
# tests/test_ocs_connections.py
from django.core.management import call_command


@pytest.mark.django_db
def test_data_migration_maps_credentials(user, django_assert_num_queries):
    # Build legacy rows directly (TenantCredential still exists pre-Task7)
    from apps.users.models import TenantCredential
    t1, t2 = _tenant("exp-1"), _tenant("exp-2")
    tm1 = TenantMembership.objects.create(user=user, tenant=t1)
    tm2 = TenantMembership.objects.create(user=user, tenant=t2)
    TenantCredential.objects.create(tenant_membership=tm1, credential_type="oauth")
    TenantCredential.objects.create(tenant_membership=tm2, credential_type="oauth")

    from apps.users.migrations import (
        _0007 := __import__("apps.users.migrations.0007_migrate_credentials_to_connections",
                            fromlist=["forward"]),
    )
    from django.apps import apps as global_apps
    _0007.forward(global_apps, None)

    tm1.refresh_from_db(); tm2.refresh_from_db()
    assert tm1.connection is not None and tm2.connection is not None
    # both OAuth memberships collapse into ONE connection per (user, provider)
    assert tm1.connection_id == tm2.connection_id
    assert tm1.connection.credential_type == "oauth"
```

> Note: the dynamic import via `__import__` is needed because the module name starts with a digit. If `django_assert_num_queries` isn't wired, drop it from the signature.

- [ ] **Step 3: Run migrations + test**

Run: `uv run python manage.py migrate users --plan | tail -5` then `uv run pytest tests/test_ocs_connections.py::test_data_migration_maps_credentials -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/users/migrations/0007_migrate_credentials_to_connections.py tests/test_ocs_connections.py
git commit -m "feat(users): data migration mapping credentials onto connections"
```

---

## Task 3: Credential resolver — resolve via connection, fail closed on stale OAuth

**Files:**
- Rewrite: `apps/users/services/credential_resolver.py`
- Modify: `apps/workspaces/tasks.py` (select_related connection)
- Test: `tests/test_ocs_connections.py`

- [ ] **Step 1: Write failing resolver tests**

```python
# tests/test_ocs_connections.py
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from django.utils import timezone
from apps.users.adapters import encrypt_credential
from apps.users.services.credential_resolver import aresolve_credential, resolve_credential


def _membership_with(user, conn, **kw):
    return TenantMembership.objects.create(user=user, tenant=_tenant(kw.pop("ext", "exp-1")), connection=conn, **kw)


@pytest.mark.django_db
def test_resolve_none_when_no_connection(user):
    tm = TenantMembership.objects.create(user=user, tenant=_tenant())
    assert resolve_credential(tm) is None


@pytest.mark.django_db
def test_resolve_api_key(user):
    conn = TenantConnection.objects.create(user=user, provider="ocs", credential_type=TenantConnection.API_KEY, encrypted_credential=encrypt_credential("k"))
    tm = _membership_with(user, conn, team_slug="acme", team_name="Acme")
    assert resolve_credential(tm) == {"type": "api_key", "value": "k"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_oauth_fails_closed_on_team_mismatch(user, mocker):
    conn = await TenantConnection.objects.acreate(user=user, provider="ocs", credential_type=TenantConnection.OAUTH)
    tm = await TenantMembership.objects.acreate(user=user, tenant=await Tenant.objects.acreate(provider="ocs", external_id="x", canonical_name="x"), connection=conn, team_slug="team-a")
    # token whose account is currently scoped to team-b
    account = MagicMock(extra_data={"team": "team-b"})
    token = MagicMock(token="tok", expires_at=timezone.now() + timedelta(hours=5), account=account)
    qs = MagicMock(); qs.select_related.return_value = qs; qs.afirst = AsyncMock(return_value=token)
    mocker.patch("apps.users.services.credential_resolver._social_token_qs", return_value=qs)
    assert await aresolve_credential(tm) is None  # fail closed

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_oauth_ok_on_team_match(user, mocker):
    conn = await TenantConnection.objects.acreate(user=user, provider="ocs", credential_type=TenantConnection.OAUTH)
    tm = await TenantMembership.objects.acreate(user=user, tenant=await Tenant.objects.acreate(provider="ocs", external_id="y", canonical_name="y"), connection=conn, team_slug="team-a")
    account = MagicMock(extra_data={"team": "team-a"})
    token = MagicMock(token="tok", expires_at=timezone.now() + timedelta(hours=5), account=account, token_secret="")
    qs = MagicMock(); qs.select_related.return_value = qs; qs.afirst = AsyncMock(return_value=token)
    mocker.patch("apps.users.services.credential_resolver._social_token_qs", return_value=qs)
    res = await aresolve_credential(tm)
    assert res == {"type": "oauth", "value": "tok"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ocs_connections.py -k resolve -q`
Expected: FAIL (resolver still references `TenantCredential`/`team_id`).

- [ ] **Step 3: Rewrite the resolver**

Replace the bodies of `resolve_credential` and `aresolve_credential` in `apps/users/services/credential_resolver.py`. Keep `_social_token_qs`, `get_social_token`, `aget_social_token`, `_aresolve_oauth_credential` unchanged. Replace the `TenantCredential` import with `TenantConnection`.

```python
from apps.users.models import TenantConnection


def _oauth_team_mismatch(membership, token_obj) -> bool:
    """True when membership's team is known and the live OAuth token is scoped elsewhere."""
    if not membership.team_slug:
        return False
    current = (getattr(token_obj.account, "extra_data", None) or {}).get("team")
    return bool(current) and current != membership.team_slug


def resolve_credential(membership) -> dict | None:
    conn = membership.connection
    if conn is None:
        return None
    if conn.credential_type == TenantConnection.API_KEY:
        try:
            return {"type": "api_key", "value": decrypt_credential(conn.encrypted_credential)}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None
    token_obj = _social_token_qs(membership.user, conn.provider).select_related("account").first()
    if not token_obj or _oauth_team_mismatch(membership, token_obj):
        return None
    return {"type": "oauth", "value": token_obj.token}


async def aresolve_credential(membership) -> dict | None:
    conn = membership.connection
    if conn is None:
        return None
    if conn.credential_type == TenantConnection.API_KEY:
        try:
            return {"type": "api_key", "value": decrypt_credential(conn.encrypted_credential)}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None
    token_obj = await _social_token_qs(membership.user, conn.provider).select_related("account", "app").afirst()
    if not token_obj or _oauth_team_mismatch(membership, token_obj):
        return None
    return await _aresolve_oauth_credential(token_obj, conn.provider)
```

> `membership.connection` must be preloaded by callers (sync attribute access in async context otherwise raises). Add `select_related("connection")` everywhere the resolver is called.

- [ ] **Step 4: Update callers in `apps/workspaces/tasks.py`**

- Line ~134: `TenantMembership.objects.select_related("tenant", "user")` → add `"connection"`.
- Line ~223: the `materialize_workspace` queryset `.select_related("user", "tenant")` → add `"connection"`, and append `.filter(archived_at__isnull=True)` to the membership filter.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_ocs_connections.py -k resolve -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/users/services/credential_resolver.py apps/workspaces/tasks.py tests/test_ocs_connections.py
git commit -m "feat(users): resolve credentials via connection with fail-closed OAuth team guard"
```

---

## Task 4: OCS team detection helper + OCS strategy team field

**Files:**
- Create: `apps/users/services/ocs_team.py`
- Modify: `apps/users/services/api_key_providers/ocs.py`
- Test: `tests/test_ocs_connections.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ocs_connections.py
import httpx
from apps.users.services.ocs_team import adetect_team_from_api_key


@pytest.mark.asyncio
async def test_detect_team_from_sessions(mocker):
    async def fake_get(url, headers=None, params=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"results": [{"team": {"name": "Acme", "slug": "acme"}}], "next": None}
        return R()
    client = mocker.MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=fake_get)
    mocker.patch("httpx.AsyncClient", return_value=client)
    assert await adetect_team_from_api_key("key", "https://ocs.example") == ("acme", "Acme")


@pytest.mark.asyncio
async def test_detect_team_none_when_no_sessions(mocker):
    async def fake_get(url, headers=None, params=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"results": [], "next": None}
        return R()
    client = mocker.MagicMock()
    client.__aenter__ = AsyncMock(return_value=client); client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=fake_get)
    mocker.patch("httpx.AsyncClient", return_value=client)
    assert await adetect_team_from_api_key("key", "https://ocs.example") is None
```

- [ ] **Step 2: Run to verify failure** — Run: `uv run pytest tests/test_ocs_connections.py -k detect -q` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `apps/users/services/ocs_team.py`**

```python
"""Detect an OCS team (slug, name) for a credential, using only verified endpoints."""
from __future__ import annotations

import logging
import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")


async def _team_from_sessions(headers: dict, base_url: str) -> tuple[str, str] | None:
    """GET /api/sessions/?page_size=1 -> results[0].team.{slug,name}, or None if no sessions."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base_url}/api/sessions/", headers=headers, params={"page_size": 1}
            )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results") or []
        if not results:
            return None
        team = results[0].get("team") or {}
        slug, name = team.get("slug"), team.get("name")
        if slug and name:
            return str(slug), str(name)
    except Exception:
        logger.warning("OCS team detection via sessions failed", exc_info=True)
    return None


async def adetect_team_from_api_key(api_key: str, base_url: str | None = None) -> tuple[str, str] | None:
    return await _team_from_sessions({"X-api-key": api_key}, base_url or _base_url())


async def adetect_team_name_from_oauth(access_token: str, base_url: str | None = None) -> str | None:
    """Best-effort friendly team name for an OAuth token (slug comes from the OIDC claim)."""
    res = await _team_from_sessions({"Authorization": f"Bearer {access_token}"}, base_url or _base_url())
    return res[1] if res else None
```

- [ ] **Step 4: Add the optional team_name field to `OCSStrategy`**

In `apps/users/services/api_key_providers/ocs.py`, extend `form_fields`:

```python
    form_fields: list[FormField] = [
        {"key": "api_key", "label": "API Key", "type": "password", "required": True, "editable_on_rotate": True},
        {"key": "team_name", "label": "Team name (auto-detected if left blank)", "type": "text", "required": False, "editable_on_rotate": False},
    ]
```

`pack_credential` is unchanged (it returns `fields["api_key"]`; the extra `team_name` is ignored there).

- [ ] **Step 5: Run tests** — Run: `uv run pytest tests/test_ocs_connections.py -k detect -q` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/users/services/ocs_team.py apps/users/services/api_key_providers/ocs.py tests/test_ocs_connections.py
git commit -m "feat(users): OCS team detection via /api/sessions/ + optional team_name field"
```

---

## Task 5: OAuth import flow → connection + per-chatbot team + un-archive

**Files:**
- Modify: `apps/users/services/tenant_resolution.py`
- Test: `tests/test_ocs_connections.py`, ensure `tests/test_ocs_tenant_resolution.py` still passes.

- [ ] **Step 1: Write failing test**

```python
# tests/test_ocs_connections.py
from unittest.mock import patch
from apps.users.services.tenant_resolution import resolve_ocs_chatbots
from allauth.socialaccount.models import SocialAccount


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_oauth_import_links_team_and_connection(user, mocker):
    await SocialAccount.objects.acreate(user=user, provider="ocs", uid="u1", extra_data={"team": "team-a"})
    experiments = [{"id": "exp-1", "name": "Bot 1"}, {"id": "exp-2", "name": "Bot 2"}]

    async def fake_get(url, headers=None, params=None):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                if "sessions" in url:
                    return {"results": [{"team": {"slug": "team-a", "name": "Team A"}}], "next": None}
                return {"results": experiments, "next": None}
        return R()
    client = mocker.MagicMock(); client.__aenter__ = AsyncMock(return_value=client); client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=fake_get)
    mocker.patch("httpx.AsyncClient", return_value=client)

    await resolve_ocs_chatbots(user, "tok")

    conns = [c async for c in TenantConnection.objects.filter(user=user, provider="ocs")]
    assert len(conns) == 1 and conns[0].credential_type == "oauth"
    tms = [tm async for tm in TenantMembership.objects.filter(user=user).select_related("connection")]
    assert len(tms) == 2
    assert all(tm.team_slug == "team-a" and tm.team_name == "Team A" for tm in tms)
    assert all(tm.connection_id == conns[0].id for tm in tms)
```

- [ ] **Step 2: Run to verify failure** — Run: `uv run pytest tests/test_ocs_connections.py -k oauth_import -q` — Expected: FAIL.

- [ ] **Step 3: Rewrite `resolve_ocs_chatbots`** (and align `resolve_commcare_domains`/`resolve_connect_opportunities` to use connections)

In `apps/users/services/tenant_resolution.py`:
- Replace the import `from apps.users.models import Tenant, TenantCredential, TenantMembership` with `TenantConnection`.
- Add helper to read the OAuth team slug from the user's OCS SocialAccount:

```python
from allauth.socialaccount.models import SocialAccount
from apps.users.services.ocs_team import adetect_team_name_from_oauth


async def _ocs_team_slug(user) -> str:
    acct = await SocialAccount.objects.filter(user=user, provider="ocs").afirst()
    return (acct.extra_data or {}).get("team", "") if acct else ""
```

- Rewrite the OCS body (remove the `/api/teams/` block entirely):

```python
async def resolve_ocs_chatbots(user, access_token: str, team_id: str | None = None) -> list[TenantMembership]:
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}

    team_slug = team_id or await _ocs_team_slug(user)
    team_name = (await adetect_team_name_from_oauth(access_token, base_url)) or team_slug

    conn, _ = await TenantConnection.objects.aget_or_create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH,
    )

    experiments: list[dict] = []
    url: str | None = f"{base_url}/api/experiments/"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise OCSAuthError(f"OCS returned {resp.status_code} — access token may have expired")
            resp.raise_for_status()
            payload = resp.json()
            experiments.extend(payload.get("results", []))
            url = payload.get("next")

    memberships = []
    for exp in experiments:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="ocs", external_id=str(exp["id"]),
            defaults={"canonical_name": exp.get("name") or str(exp["id"])},
        )
        tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
        tm.connection = conn
        tm.team_slug = team_slug
        tm.team_name = team_name
        tm.archived_at = None
        await tm.asave(update_fields=["connection", "team_slug", "team_name", "archived_at"])
        memberships.append(tm)

    logger.info("Resolved %d OCS chatbots for user %s (team %s)", len(memberships), user.email, team_slug)
    return memberships
```

- In `resolve_commcare_domains` and `resolve_connect_opportunities`, replace the per-membership `TenantCredential.objects.aget_or_create(...)` block with:

```python
    conn, _ = await TenantConnection.objects.aget_or_create(
        user=user, provider=<"commcare"|"commcare_connect">, credential_type=TenantConnection.OAUTH,
    )
```

(create once before the loop), and inside the loop after `aget_or_create` the membership:

```python
        tm.connection = conn
        tm.archived_at = None
        await tm.asave(update_fields=["connection", "archived_at"])
```

(team_slug/team_name stay "" for these providers.)

- [ ] **Step 4: Run tests** — Run: `uv run pytest tests/test_ocs_connections.py tests/test_ocs_tenant_resolution.py -q` — Expected: PASS (the existing resolution test no longer mocks `/api/teams/`; it now also receives a sessions call, which the permissive mock answers — confirm green).

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/tenant_resolution.py tests/test_ocs_connections.py
git commit -m "feat(users): OAuth import creates one connection per provider, stamps chatbot team"
```

---

## Task 6: Connection endpoints, API-key add, onboarding, disconnect, listings

**Files:**
- Modify: `apps/users/views.py`, `apps/users/auth_urls.py`, `apps/users/auth_views.py`
- Test: `tests/test_ocs_connections.py`, `tests/test_tenant_api.py`, `tests/test_tenant_credentials_patch.py`

- [ ] **Step 1: Write failing API tests**

```python
# tests/test_ocs_connections.py
import json
from asgiref.sync import sync_to_async
from django.test import AsyncClient


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_api_key_add_creates_one_connection_with_team(user, mocker):
    await sync_to_async(user.set_password)("pw"); await user.asave()
    mocker.patch("apps.users.services.api_key_providers.ocs.OCSStrategy.verify_and_discover",
                 AsyncMock(return_value=[__import__("apps.users.services.api_key_providers.base", fromlist=["TenantDescriptor"]).TenantDescriptor("exp-1", "Bot 1")]))
    mocker.patch("apps.users.views.adetect_team_from_api_key", AsyncMock(return_value=("acme", "Acme")))
    client = AsyncClient(); await sync_to_async(client.login)(email=user.email, password="pw")
    resp = await client.post("/api/auth/connections/", data=json.dumps({"provider": "ocs", "fields": {"api_key": "k"}}), content_type="application/json")
    assert resp.status_code == 201
    conns = [c async for c in TenantConnection.objects.filter(user=user, provider="ocs", credential_type="api_key")]
    assert len(conns) == 1
    tm = await TenantMembership.objects.select_related("connection").aget(user=user, tenant__external_id="exp-1")
    assert tm.team_slug == "acme" and tm.team_name == "Acme" and tm.connection_id == conns[0].id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_remove_connection_archives_memberships(user, mocker):
    conn = await TenantConnection.objects.acreate(user=user, provider="ocs", credential_type="api_key", encrypted_credential="e")
    t = await Tenant.objects.acreate(provider="ocs", external_id="exp-9", canonical_name="B")
    tm = await TenantMembership.objects.acreate(user=user, tenant=t, connection=conn, team_slug="acme")
    await sync_to_async(user.set_password)("pw"); await user.asave()
    client = AsyncClient(); await sync_to_async(client.login)(email=user.email, password="pw")
    resp = await client.delete(f"/api/auth/connections/{conn.id}/")
    assert resp.status_code == 200
    tm = await TenantMembership.objects.aget(id=tm.id)
    assert tm.archived_at is not None and tm.connection_id is None
    assert not await TenantConnection.objects.filter(id=conn.id).aexists()
```

- [ ] **Step 2: Run to verify failure** — Run: `uv run pytest tests/test_ocs_connections.py -k "api_key_add or remove_connection" -q` — Expected: FAIL (endpoints don't exist).

- [ ] **Step 3: Implement views** in `apps/users/views.py`

- Add import: `from apps.users.models import Tenant, TenantConnection, TenantMembership` and `from apps.users.services.ocs_team import adetect_team_from_api_key`. Remove `TenantCredential` and `_extract_ocs_team_info` and the `httpx`/`/api/teams/` code.
- Replace `_persist_api_key_memberships` with a connection-creating version:

```python
@sync_to_async
def _persist_api_key_connection(user, provider, descriptors, encrypted, team_slug, team_name):
    rows = []
    with transaction.atomic():
        conn = TenantConnection.objects.create(
            user=user, provider=provider, credential_type=TenantConnection.API_KEY,
            encrypted_credential=encrypted,
        )
        for desc in descriptors:
            tenant, _ = Tenant.objects.get_or_create(
                provider=provider, external_id=desc.external_id,
                defaults={"canonical_name": desc.canonical_name},
            )
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
            tm.connection = conn
            tm.team_slug = team_slug
            tm.team_name = team_name
            tm.archived_at = None
            tm.save(update_fields=["connection", "team_slug", "team_name", "archived_at"])
            rows.append({"membership_id": str(tm.id), "tenant_id": tenant.external_id, "tenant_name": tenant.canonical_name})
    return rows
```

- Rewrite `tenant_credential_list_view` as connection list/create. GET returns nested connections (exclude archived memberships); POST does team detection then calls `_persist_api_key_connection`:

```python
# GET branch
results = []
async for conn in TenantConnection.objects.filter(user=user).order_by("-created_at"):
    chatbots = []
    async for tm in conn.memberships.filter(archived_at__isnull=True).select_related("tenant"):
        chatbots.append({"membership_id": str(tm.id), "tenant_id": tm.tenant.external_id,
                         "tenant_name": tm.tenant.canonical_name, "team_slug": tm.team_slug, "team_name": tm.team_name})
    results.append({"connection_id": str(conn.id), "provider": conn.provider,
                    "credential_type": conn.credential_type, "chatbots": chatbots})
return JsonResponse(results, safe=False)

# POST branch — after verify_and_discover + encrypt:
team_slug, team_name = "", ""
if provider == "ocs":
    detected = await adetect_team_from_api_key(fields.get("api_key", ""))
    if detected:
        team_slug, team_name = detected
    else:
        team_name = (fields.get("team_name") or "").strip()
        if not team_name:
            return JsonResponse({"error": "Could not detect the OCS team; enter a team name."}, status=400)
memberships_payload = await _persist_api_key_connection(user, provider, descriptors, encrypted, team_slug, team_name)
return JsonResponse({"memberships": memberships_payload}, status=201)
```

- Replace `tenant_credential_detail_view` with `connection_detail_view(request, connection_id)`:
  - PATCH: load `TenantConnection` by `(id=connection_id, user=user)`; resolve strategy via its provider; `verify_for_tenant` against one of its memberships' `external_id`; `encrypt`; update `encrypted_credential`.
  - DELETE: load the connection; in a `@sync_to_async transaction.atomic`, set `archived_at=now()` and `connection=None` on its non-archived memberships, then delete the connection. Return `{"status": "removed"}`.

- [ ] **Step 4: Wire URLs** in `apps/users/auth_urls.py`

Replace the two `tenant-credentials/...` paths with:

```python
    path("connections/", tenant_credential_list_view, name="connections"),
    path("connections/<str:connection_id>/", connection_detail_view, name="connection-detail"),
```

(import `connection_detail_view`; keep `api-key-providers/`.) Update the import block accordingly.

- [ ] **Step 5: Update `auth_views.py`**

- `me_view` and `login_view`: change `credential__isnull=False` → `connection__isnull=False, archived_at__isnull=True`.
- `disconnect_provider_view`: after `tokens.delete()`, also archive + drop the OAuth connection:

```python
    # archive memberships served by this provider's OAuth connection, then remove it
    oauth_conns = TenantConnection.objects.filter(user=request.user, provider=provider_id, credential_type=TenantConnection.OAUTH)
    TenantMembership.objects.filter(connection__in=oauth_conns).update(archived_at=timezone.now(), connection=None)
    oauth_conns.delete()
```

(import `TenantConnection`, `timezone`. Note `provider_id` may be the SocialApp provider id; match the same value used to create the OAuth connection — for OCS that is `"ocs"`.)

- [ ] **Step 6: Fix existing tests** — update `tests/test_tenant_api.py` and `tests/test_tenant_credentials_patch.py` to hit `/api/auth/connections/` and assert via `tm.connection`/connection rotate. Run: `uv run pytest tests/test_ocs_connections.py tests/test_tenant_api.py tests/test_tenant_credentials_patch.py -q` — Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/users/views.py apps/users/auth_urls.py apps/users/auth_views.py tests/
git commit -m "feat(users): connection endpoints, API-key team detection, archive-on-remove"
```

---

## Task 7: Delete `TenantCredential`

**Files:**
- Modify: `apps/users/models.py`
- Create: `apps/users/migrations/0008_delete_tenantcredential.py`

- [ ] **Step 1: Confirm no references remain**

Run: `rg -n "TenantCredential|tm\.credential\b|\.credential__|credential__isnull" apps/ tests/`
Expected: only the model definition + the data migration's `apps.get_model("users","TenantCredential")` (allowed). Fix any stragglers.

- [ ] **Step 2: Delete the model + migrate**

Remove the `TenantCredential` class from `apps/users/models.py`. Then:
Run: `uv run python manage.py makemigrations users -n delete_tenantcredential`
Expected: creates `0008_delete_tenantcredential.py` with `DeleteModel`.

- [ ] **Step 3: Run full migration + suite**

Run: `uv run python manage.py migrate && uv run pytest tests/test_ocs_connections.py tests/test_auth.py tests/test_tenant_api.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/users/models.py apps/users/migrations/0008_delete_tenantcredential.py
git commit -m "refactor(users): remove TenantCredential, superseded by TenantConnection"
```

---

## Task 8: Frontend — connection-grouped Connections page

**Files:**
- Modify: `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx`, `frontend/src/components/ApiConnectionDialog/ApiConnectionDialog.tsx`

- [ ] **Step 1: Update the dialog** — `ApiConnectionDialog.tsx`: it already renders fields dynamically from the provider schema, so the new optional `team_name` field appears automatically for OCS. Change the POST target from `/api/auth/tenant-credentials/` to `/api/auth/connections/`, and the PATCH target to `/api/auth/connections/${editing.connection_id}/`. Update the `ApiKeyConnection` interface to the connection shape (`connection_id`, `provider`, `credential_type`, `chatbots: {membership_id, tenant_id, tenant_name, team_slug, team_name}[]`).

- [ ] **Step 2: Update the page** — `ConnectionsPage.tsx`: change `fetchConnections` to `GET /api/auth/connections/`; render one card per connection with its team label (from the first chatbot's `team_name`, falling back to the provider) and a list of its chatbots; wire Edit (rotate) and Remove (`DELETE /api/auth/connections/${connection_id}/`) to the connection. Preserve/extend `data-testid`s: `connection-card-<id>`, `connection-team-<id>`, `rotate-connection-<id>`, `remove-connection-<id>`, `add-connection-button`.

- [ ] **Step 3: Build + lint**

Run: `cd frontend && bun run build && bun run lint`
Expected: type-check + build pass, lint clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): connection-grouped Connections page with team labels"
```

---

## Task 9: Full verification

- [ ] **Step 1: Backend suite** — Run: `uv run pytest -q` — Expected: all pass.
- [ ] **Step 2: Lint/format** — Run: `uv run ruff check . && uv run ruff format --check .` — Expected: clean.
- [ ] **Step 3: Migrations sanity** — Run: `uv run python manage.py makemigrations --check --dry-run` — Expected: "No changes detected".
- [ ] **Step 4: Frontend** — Run: `cd frontend && bun run build && bun run lint` — Expected: clean.
- [ ] **Step 5: Commit any fixups**, then proceed to PR.

---

## Self-review notes (for the implementer)

- The resolver reads `membership.connection` and `membership.team_slug`/`team_name` — every caller must `select_related("connection")` (sync attribute access on an unloaded relation throws in async).
- `team_slug` empty ⇒ OAuth guard skipped (correct for CommCare/Connect and legacy rows).
- Do not add any OCS endpoint other than `/api/experiments/`, `/api/sessions/`, `/o/userinfo/`, `/o/token/`.
- The data migration must run before `DeleteModel` (Task 7 depends on Task 2).
