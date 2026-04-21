# OCS Multi-Team OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a Scout user to authenticate to Open Chat Studio multiple times so that tokens for multiple OCS teams are stored side-by-side, enabling tenant access across teams.

**Architecture:** Keep `SocialAccount` as the single OCS identity link (one per user). Stop using allauth's `SocialToken` as OCS's token store. Introduce `OCSTeamToken(user, ocs_team_id, access_token, refresh_token, expires_at, app)` as the authoritative per-team token store. On OAuth callback, identify the team the token was granted for and upsert into `OCSTeamToken`. OCS `Tenant` rows gain a `team_external_id` attribute (or equivalent) so the credential resolver can map `Tenant → team → OCSTeamToken`. Token refresh and the disconnect endpoint switch to the new model for OCS only; CommCare/Connect stay on allauth.

**Tech Stack:** Django 5 (async ORM), django-allauth, httpx, Fernet (for at-rest refresh-token encryption), pytest-asyncio.

---

## Open Design Questions (resolve in Task 0 before coding)

These must be answered by poking the live OCS API before the data model is finalised. Task 0 is a spike, not implementation.

1. **How do we identify the team a just-issued OCS token belongs to?**
   Candidates:
   - Claim in the ID token / userinfo (e.g. `team_slug`, `tenant`). Current `OCSProvider.extract_common_fields` (`apps/users/providers/ocs/provider.py:34`) only reads user claims; check if `sub`/userinfo carries team.
   - A dedicated OCS endpoint (e.g. `GET /api/teams/current/` or similar).
   - Inspecting the first page of `/api/experiments/` and reading the `team` field per experiment (the resolver already pages through this at `apps/users/services/tenant_resolution.py:113`).
   - Decoding the access token as a JWT if it is one.
2. **Does OCS re-prompt the team picker on each authorize request?**
   If not, we need `prompt=login` / `prompt=consent` on subsequent authorize redirects, or a team-specific authorize param.
3. **Does the OCS `/o/token/` refresh grant return a team-scoped token, or must the team be selected again interactively?**
   If refresh does not preserve team, we cannot transparently refresh — users must re-consent per team on token expiry.
4. **Is the refresh token re-emitted on every token grant, or only on first issue?**
   Determines whether `OCSTeamToken.refresh_token` is mandatory or nullable.

**Exit criterion for Task 0:** a short addendum to this document (added under a new `## Design Decisions` heading) answering all four questions with evidence (HTTP request/response samples or OCS docs link). The remaining tasks assume decisions equivalent to: *"team id is read from the first page of `/api/experiments/` after token grant; authorize URL includes `prompt=consent`; refresh preserves team; refresh token is re-emitted."* Adjust tasks if reality differs.

---

## File Structure

- Create: `apps/users/models.py` — add `OCSTeamToken` model (appended to existing file).
- Create: `apps/users/migrations/000N_add_ocs_team_token.py` — schema migration.
- Create: `apps/users/migrations/000M_backfill_ocs_team_tokens.py` — data migration from `SocialToken` → `OCSTeamToken` for existing OCS users.
- Create: `apps/users/services/ocs_team.py` — helpers: `identify_team_from_token()`, `upsert_team_token()`, `get_team_token()`, `arefresh_team_token()`.
- Modify: `apps/users/providers/ocs/views.py` — add `prompt=consent` to authorize URL; expose the raw `token` on the sociallogin so the signal can read refresh + expiry without re-reading `SocialToken`.
- Modify: `apps/users/signals.py` — in the OCS branch, identify team, upsert `OCSTeamToken`, then call `resolve_ocs_chatbots` scoped to that team.
- Modify: `apps/users/services/tenant_resolution.py` — `resolve_ocs_chatbots` becomes `resolve_ocs_chatbots_for_team(user, access_token, team_external_id)`; also tags `Tenant` rows with `team_external_id`.
- Modify: `apps/users/services/credential_resolver.py` — OCS branch of `aresolve_credential` looks up `OCSTeamToken` via `tenant.team_external_id`, not `SocialToken`.
- Modify: `apps/users/services/token_refresh.py` — add `arefresh_ocs_team_token(team_token)` that knows how to persist back into `OCSTeamToken`.
- Modify: `apps/users/auth_views.py` — `disconnect_provider_view` accepts an optional team identifier for OCS; `providers_view` reports per-team status for OCS; `me_view` lazy resolver invokes the team-aware OCS path.
- Modify: `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` — render a list of connected OCS teams with per-team disconnect; add a "Connect another team" button that restarts the OAuth flow.

---

## Task 0: Spike — verify OCS team identification

**Files:** (none — doc-only)

- [ ] **Step 1: Capture a live OCS OAuth round-trip**

In a dev environment with OCS credentials set, go through the existing OAuth flow, capture:
- The full userinfo response (currently logged in `OCSOAuth2Adapter.complete_login`, `apps/users/providers/ocs/views.py:30`).
- The first page of `GET {OCS_URL}/api/experiments/` using the issued token, inspecting whether each experiment carries a `team`, `team_slug`, or similar field.
- A `POST {OCS_URL}/o/token/` refresh response, inspecting whether the new access token is usable against experiments outside / inside the original team.

- [ ] **Step 2: Record findings**

Append a `## Design Decisions` section to this plan file with:
- Chosen team identifier source (userinfo claim name, or experiments-page field name).
- Whether `prompt=consent` is needed.
- Whether refresh preserves team scope.
- Whether refresh tokens rotate.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-04-21-ocs-multi-team-oauth.md
git commit -m "docs(plan): record OCS multi-team OAuth design decisions"
```

---

## Task 1: Add `OCSTeamToken` model

**Files:**
- Modify: `apps/users/models.py` (append new class)
- Test: `tests/test_ocs_team_token_model.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ocs_team_token_model.py
import pytest
from django.utils import timezone
from datetime import timedelta

from apps.users.models import OCSTeamToken


@pytest.mark.django_db
def test_ocs_team_token_unique_per_user_and_team(user):
    OCSTeamToken.objects.create(
        user=user,
        team_external_id="team-alpha",
        team_name="Team Alpha",
        access_token="token-1",
        refresh_token="refresh-1",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    with pytest.raises(Exception):
        OCSTeamToken.objects.create(
            user=user,
            team_external_id="team-alpha",
            team_name="Team Alpha (dup)",
            access_token="token-2",
            refresh_token="refresh-2",
            expires_at=timezone.now() + timedelta(hours=1),
        )


@pytest.mark.django_db
def test_ocs_team_token_allows_multiple_teams_for_same_user(user):
    OCSTeamToken.objects.create(
        user=user,
        team_external_id="team-alpha",
        team_name="Team Alpha",
        access_token="token-1",
        refresh_token="refresh-1",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    OCSTeamToken.objects.create(
        user=user,
        team_external_id="team-beta",
        team_name="Team Beta",
        access_token="token-2",
        refresh_token="refresh-2",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    assert OCSTeamToken.objects.filter(user=user).count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ocs_team_token_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'OCSTeamToken'`.

- [ ] **Step 3: Add the model**

Append to `apps/users/models.py`:

```python
class OCSTeamToken(models.Model):
    """OAuth token for a single OCS team, belonging to a user.

    OCS issues team-scoped tokens. A user may connect multiple teams, each
    producing a separate token. This table is the authoritative store for
    OCS OAuth tokens; allauth's SocialToken is not used for OCS.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ocs_team_tokens",
    )
    team_external_id = models.CharField(max_length=255)
    team_name = models.CharField(max_length=255)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    app = models.ForeignKey(
        "socialaccount.SocialApp",
        on_delete=models.CASCADE,
        related_name="ocs_team_tokens",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["user", "team_external_id"]]
        ordering = ["team_name"]

    def __str__(self) -> str:
        return f"OCSTeamToken({self.user_id}, {self.team_external_id})"
```

- [ ] **Step 4: Generate + apply migration**

```bash
uv run python manage.py makemigrations users --name add_ocs_team_token
uv run python manage.py migrate users
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_ocs_team_token_model.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/users/models.py apps/users/migrations/ tests/test_ocs_team_token_model.py
git commit -m "feat(users): add OCSTeamToken model for multi-team OAuth"
```

---

## Task 2: Add `team_external_id` to `Tenant`

**Files:**
- Modify: `apps/users/models.py` (add field on `Tenant`)
- Test: extend `tests/test_ocs_tenant_resolution.py`

The resolver must be able to map an OCS `Tenant` (one per experiment) back to the team that owns it, so the credential resolver can pick the right `OCSTeamToken`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_ocs_tenant_resolution.py

@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resolve_ocs_chatbots_for_team_tags_tenant_with_team(user, httpx_mock):
    httpx_mock.add_response(
        url="https://www.openchatstudio.com/api/experiments/",
        json={
            "next": None,
            "results": [
                {"id": "exp-1", "name": "Bot A"},
                {"id": "exp-2", "name": "Bot B"},
            ],
        },
    )
    from apps.users.services.tenant_resolution import resolve_ocs_chatbots_for_team

    await resolve_ocs_chatbots_for_team(user, "access-token", team_external_id="team-alpha")

    tenants = [t async for t in Tenant.objects.filter(provider="ocs").order_by("external_id")]
    assert all(t.team_external_id == "team-alpha" for t in tenants)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_ocs_tenant_resolution.py -k team -v`
Expected: FAIL — attribute/function does not exist.

- [ ] **Step 3: Add `team_external_id` to `Tenant`**

In `apps/users/models.py`, modify the `Tenant` class:

```python
class Tenant(models.Model):
    ...
    team_external_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="For providers with team-scoped tokens (OCS), identifies the owning team.",
    )
```

- [ ] **Step 4: Generate migration**

```bash
uv run python manage.py makemigrations users --name add_tenant_team_external_id
uv run python manage.py migrate users
```

- [ ] **Step 5: Commit**

```bash
git add apps/users/models.py apps/users/migrations/
git commit -m "feat(users): add team_external_id to Tenant for team-scoped providers"
```

*(The resolver function that actually sets it is implemented in Task 4. We're splitting schema from behaviour.)*

---

## Task 3: Team identification helper

**Files:**
- Create: `apps/users/services/ocs_team.py`
- Test: `tests/test_ocs_team_service.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ocs_team_service.py
import pytest
from apps.users.services.ocs_team import identify_team_from_experiments_page


def test_identify_team_from_experiments_page_reads_first_experiment():
    page = {
        "results": [
            {"id": "exp-1", "name": "Bot", "team": {"slug": "team-alpha", "name": "Alpha"}},
        ],
    }
    team_id, team_name = identify_team_from_experiments_page(page)
    assert team_id == "team-alpha"
    assert team_name == "Alpha"


def test_identify_team_from_experiments_page_empty_raises():
    with pytest.raises(ValueError):
        identify_team_from_experiments_page({"results": []})
```

*If Task 0 established that team identity comes from userinfo instead of the experiments page, rewrite the helper to read the userinfo claim; the shape of this test stays the same.*

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_ocs_team_service.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

```python
# apps/users/services/ocs_team.py
"""Services for managing OCS team-scoped OAuth tokens."""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone

from allauth.socialaccount.models import SocialApp

from apps.users.models import OCSTeamToken

logger = logging.getLogger(__name__)


def identify_team_from_experiments_page(page: dict) -> tuple[str, str]:
    """Return (team_external_id, team_name) from the first experiment in a page."""
    results = page.get("results") or []
    if not results:
        raise ValueError("OCS experiments response is empty; cannot identify team")
    team = results[0].get("team") or {}
    team_id = team.get("slug") or team.get("id")
    team_name = team.get("name") or str(team_id)
    if not team_id:
        raise ValueError(f"First experiment has no team field: {results[0]!r}")
    return str(team_id), team_name


async def fetch_first_experiments_page(access_token: str) -> dict:
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base_url}/api/experiments/",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
    return resp.json()


async def aupsert_team_token(
    *, user, app, team_external_id: str, team_name: str,
    access_token: str, refresh_token: str, expires_in: int | None,
) -> OCSTeamToken:
    expires_at = (
        timezone.now() + timedelta(seconds=expires_in) if expires_in else None
    )
    obj, _ = await OCSTeamToken.objects.aupdate_or_create(
        user=user,
        team_external_id=team_external_id,
        defaults={
            "team_name": team_name,
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "expires_at": expires_at,
            "app": app,
        },
    )
    return obj
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_ocs_team_service.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/users/services/ocs_team.py tests/test_ocs_team_service.py
git commit -m "feat(users): add OCS team identification + token upsert helpers"
```

---

## Task 4: Rework `resolve_ocs_chatbots` to be team-scoped

**Files:**
- Modify: `apps/users/services/tenant_resolution.py`
- Test: extend `tests/test_ocs_tenant_resolution.py`

Rename `resolve_ocs_chatbots` → `resolve_ocs_chatbots_for_team` and require a `team_external_id` argument. Stamp it on every `Tenant` created.

- [ ] **Step 1: Modify the test added in Task 2 so it runs**

Already written in Task 2 — the test expects a function `resolve_ocs_chatbots_for_team(user, access_token, team_external_id)`.

- [ ] **Step 2: Update the resolver**

In `apps/users/services/tenant_resolution.py`, replace `resolve_ocs_chatbots` with:

```python
async def resolve_ocs_chatbots_for_team(
    user, access_token: str, team_external_id: str
) -> list[TenantMembership]:
    """Fetch experiments for a team and upsert TenantMembership rows tagged with the team."""
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")
    url: str | None = f"{base_url}/api/experiments/"

    experiments: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code in (401, 403):
                raise OCSAuthError(
                    f"OCS returned {resp.status_code} — access token may have expired"
                )
            resp.raise_for_status()
            payload = resp.json()
            experiments.extend(payload.get("results", []))
            url = payload.get("next")

    memberships = []
    for exp in experiments:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="ocs",
            external_id=str(exp["id"]),
            defaults={
                "canonical_name": exp.get("name") or str(exp["id"]),
                "team_external_id": team_external_id,
            },
        )
        tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
        await TenantCredential.objects.aget_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)

    logger.info(
        "Resolved %d OCS chatbots for user %s (team %s)",
        len(memberships), user.email, team_external_id,
    )
    return memberships
```

Delete the original `resolve_ocs_chatbots`. Fix up all imports in `apps/users/signals.py` and `apps/users/auth_views.py` to call a wrapper (see Task 5) rather than this function directly — those call sites do not have `team_external_id` on hand.

- [ ] **Step 3: Run resolver tests**

Run: `uv run pytest tests/test_ocs_tenant_resolution.py -v`
Expected: PASS (all including Task 2's team-tagging test).

- [ ] **Step 4: Commit**

```bash
git add apps/users/services/tenant_resolution.py tests/test_ocs_tenant_resolution.py
git commit -m "refactor(tenant-resolution): scope OCS chatbot resolver to a single team"
```

---

## Task 5: OAuth callback wiring — identify team and persist `OCSTeamToken`

**Files:**
- Modify: `apps/users/signals.py`
- Test: `tests/test_ocs_signal_multi_team.py` (new)

The signal is where OAuth completion lands. It needs to:
1. Read the freshly issued access + refresh token off the `sociallogin`.
2. Call `fetch_first_experiments_page` to identify the team.
3. Upsert `OCSTeamToken` via `aupsert_team_token`.
4. Call `resolve_ocs_chatbots_for_team` passing the identified team.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ocs_signal_multi_team.py
import pytest
from datetime import timedelta
from django.utils import timezone

from apps.users.models import OCSTeamToken


@pytest.mark.django_db(transaction=True)
def test_signal_creates_ocs_team_token(user, mock_ocs_oauth_flow):
    """After OCS OAuth, an OCSTeamToken row exists for the selected team."""
    mock_ocs_oauth_flow(
        user=user,
        team_external_id="team-alpha",
        team_name="Alpha",
        access_token="access-1",
        refresh_token="refresh-1",
        experiments=[{"id": "exp-1", "name": "Bot", "team": {"slug": "team-alpha", "name": "Alpha"}}],
    )
    tokens = list(OCSTeamToken.objects.filter(user=user))
    assert len(tokens) == 1
    assert tokens[0].team_external_id == "team-alpha"
    assert tokens[0].access_token == "access-1"


@pytest.mark.django_db(transaction=True)
def test_signal_second_team_creates_second_row(user, mock_ocs_oauth_flow):
    mock_ocs_oauth_flow(user=user, team_external_id="team-alpha", team_name="Alpha",
                       access_token="access-1", refresh_token="refresh-1",
                       experiments=[{"id": "exp-1", "name": "Bot", "team": {"slug": "team-alpha", "name": "Alpha"}}])
    mock_ocs_oauth_flow(user=user, team_external_id="team-beta", team_name="Beta",
                       access_token="access-2", refresh_token="refresh-2",
                       experiments=[{"id": "exp-2", "name": "Bot", "team": {"slug": "team-beta", "name": "Beta"}}])
    assert OCSTeamToken.objects.filter(user=user).count() == 2
```

*`mock_ocs_oauth_flow` is a new fixture that constructs a fake `sociallogin` and fires `resolve_tenant_on_social_login`; implement it in `conftest.py`. See Step 2.*

- [ ] **Step 2: Add the fixture**

```python
# tests/conftest.py (add)
import pytest
from unittest.mock import patch
from types import SimpleNamespace

@pytest.fixture
def mock_ocs_oauth_flow():
    """Simulate an OCS OAuth completion by firing resolve_tenant_on_social_login."""
    from apps.users.signals import resolve_tenant_on_social_login

    def _invoke(*, user, team_external_id, team_name, access_token, refresh_token, experiments):
        sociallogin = SimpleNamespace(
            user=user,
            account=SimpleNamespace(provider="ocs"),
            token=SimpleNamespace(
                token=access_token,
                token_secret=refresh_token,
                expires_at=None,
                app=SimpleNamespace(client_id="test-client", secret="test-secret"),
            ),
        )
        page = {"next": None, "results": experiments}
        with patch(
            "apps.users.services.ocs_team.fetch_first_experiments_page",
            return_value=page,
        ), patch(
            "apps.users.services.tenant_resolution.resolve_ocs_chatbots_for_team",
            return_value=[],
        ):
            resolve_tenant_on_social_login(request=None, sociallogin=sociallogin)
    return _invoke
```

- [ ] **Step 3: Run test, verify it fails**

Run: `uv run pytest tests/test_ocs_signal_multi_team.py -v`
Expected: FAIL — signal still calls the old `resolve_ocs_chatbots` and never writes `OCSTeamToken`.

- [ ] **Step 4: Update the signal**

In `apps/users/signals.py`, replace the `elif provider == "ocs":` branch:

```python
    elif provider == "ocs":
        try:
            async_to_sync(_handle_ocs_oauth)(sociallogin)
        except Exception:
            logger.warning("Failed to handle OCS OAuth completion", exc_info=True)
```

And add below:

```python
async def _handle_ocs_oauth(sociallogin) -> None:
    from apps.users.services.ocs_team import (
        aupsert_team_token,
        fetch_first_experiments_page,
        identify_team_from_experiments_page,
    )
    from apps.users.services.tenant_resolution import resolve_ocs_chatbots_for_team

    token = sociallogin.token
    page = await fetch_first_experiments_page(token.token)
    team_id, team_name = identify_team_from_experiments_page(page)

    expires_in = None
    if token.expires_at:
        delta = (token.expires_at - timezone.now()).total_seconds()
        if delta > 0:
            expires_in = int(delta)

    await aupsert_team_token(
        user=sociallogin.user,
        app=token.app,
        team_external_id=team_id,
        team_name=team_name,
        access_token=token.token,
        refresh_token=token.token_secret or "",
        expires_in=expires_in,
    )
    await resolve_ocs_chatbots_for_team(sociallogin.user, token.token, team_id)
```

Add `from django.utils import timezone` to the top of the file.

- [ ] **Step 5: Run tests, verify they pass**

Run: `uv run pytest tests/test_ocs_signal_multi_team.py tests/test_ocs_tenant_resolution.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/users/signals.py tests/test_ocs_signal_multi_team.py tests/conftest.py
git commit -m "feat(oauth): persist per-team OCS tokens on social-login signal"
```

---

## Task 6: Swap credential resolver to read `OCSTeamToken`

**Files:**
- Modify: `apps/users/services/credential_resolver.py`
- Modify: `apps/users/services/token_refresh.py` (add team-token refresh)
- Test: `tests/test_credential_resolver_ocs.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_credential_resolver_ocs.py
import pytest
from datetime import timedelta
from django.utils import timezone

from apps.users.models import OCSTeamToken, Tenant, TenantCredential, TenantMembership
from apps.users.services.credential_resolver import aresolve_credential


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_aresolve_credential_ocs_picks_team_token(user, ocs_social_app):
    tenant = await Tenant.objects.acreate(
        provider="ocs", external_id="exp-1", canonical_name="Bot",
        team_external_id="team-alpha",
    )
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)
    await TenantCredential.objects.acreate(
        tenant_membership=tm, credential_type=TenantCredential.OAUTH,
    )
    await OCSTeamToken.objects.acreate(
        user=user, team_external_id="team-alpha", team_name="Alpha",
        access_token="right-token", refresh_token="r", app=ocs_social_app,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    await OCSTeamToken.objects.acreate(
        user=user, team_external_id="team-beta", team_name="Beta",
        access_token="wrong-token", refresh_token="r", app=ocs_social_app,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    cred = await aresolve_credential(tm)
    assert cred == {"type": "oauth", "value": "right-token"}
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_credential_resolver_ocs.py -v`
Expected: FAIL — resolver still consults `SocialToken` and returns None/wrong token.

- [ ] **Step 3: Update the resolver**

In `apps/users/services/credential_resolver.py`, replace the OCS path inside `aresolve_credential`:

```python
    provider = membership.tenant.provider
    if provider == "ocs":
        return await _aresolve_ocs_team_credential(membership)

    token_obj = await _social_token_qs(membership.user, provider).select_related("app").afirst()
    ...
```

Add:

```python
async def _aresolve_ocs_team_credential(membership) -> dict | None:
    from apps.users.models import OCSTeamToken
    from apps.users.services.token_refresh import (
        arefresh_ocs_team_token, token_needs_refresh,
    )

    team_id = membership.tenant.team_external_id
    if not team_id:
        logger.warning("OCS tenant %s has no team_external_id", membership.tenant_id)
        return None
    try:
        tt = await OCSTeamToken.objects.select_related("app").aget(
            user=membership.user, team_external_id=team_id,
        )
    except OCSTeamToken.DoesNotExist:
        return None

    token_value = tt.access_token
    if token_needs_refresh(tt.expires_at) and tt.refresh_token:
        try:
            token_value = await arefresh_ocs_team_token(tt)
        except Exception:
            logger.warning("OCS team-token refresh failed; using existing token", exc_info=True)
    return {"type": "oauth", "value": token_value}
```

- [ ] **Step 4: Add `arefresh_ocs_team_token`**

In `apps/users/services/token_refresh.py`:

```python
async def arefresh_ocs_team_token(team_token) -> str:
    """Refresh an OCSTeamToken in-place; returns the new access token."""
    from datetime import timedelta
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _ocs_token_url(),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": team_token.refresh_token,
                    "client_id": team_token.app.client_id,
                    "client_secret": team_token.app.secret,
                },
            )
            resp.raise_for_status()
    except Exception as e:
        raise TokenRefreshError(f"Failed to refresh OCS team token: {e}") from e
    data = resp.json()
    team_token.access_token = data["access_token"]
    if data.get("refresh_token"):
        team_token.refresh_token = data["refresh_token"]
    if data.get("expires_in"):
        team_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    await team_token.asave()
    return team_token.access_token
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `uv run pytest tests/test_credential_resolver_ocs.py tests/test_oauth_tokens.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/users/services/credential_resolver.py apps/users/services/token_refresh.py tests/test_credential_resolver_ocs.py
git commit -m "feat(resolver): resolve OCS credentials from per-team token store"
```

---

## Task 7: Data migration — backfill existing `SocialToken` → `OCSTeamToken`

**Files:**
- Create: `apps/users/migrations/000M_backfill_ocs_team_tokens.py`
- Test: `tests/test_ocs_team_token_backfill.py` (new)

Existing OCS users have a `SocialToken` and a set of `Tenant(provider="ocs")` rows without `team_external_id`. The migration must:
1. For each user with an OCS `SocialToken`, call the OCS experiments endpoint to identify their team. **This requires a live network call during migration — not acceptable in CI.** Instead, schedule a management command the migration can be safely re-run from, and have the data migration only populate rows for tokens whose first experiment is already cached locally. For users with no local experiments, leave `team_external_id` blank and surface a warning; these users will need to re-auth.

- [ ] **Step 1: Write the backfill test**

```python
# tests/test_ocs_team_token_backfill.py
import pytest
from django.core.management import call_command

@pytest.mark.django_db(transaction=True)
def test_backfill_creates_team_tokens_for_known_tenants(...):
    ...
```

*(Fill in concretely once Task 0 fixes the team-identification strategy — the test fixture shape depends on whether team info is in experiments response or userinfo.)*

- [ ] **Step 2–4**

Implement a `backfill_ocs_team_tokens` management command that iterates `SocialToken` for `account__provider="ocs"`, calls `fetch_first_experiments_page`, identifies the team, creates `OCSTeamToken`, updates `Tenant.team_external_id`, then deletes the `SocialToken`. Add a dry-run mode.

- [ ] **Step 5: Commit**

```bash
git add apps/users/management/commands/backfill_ocs_team_tokens.py apps/users/migrations/ tests/test_ocs_team_token_backfill.py
git commit -m "feat(ops): backfill OCSTeamToken from legacy SocialToken rows"
```

---

## Task 8: Authorize URL — prompt for team picker on repeat connects

**Files:**
- Modify: `apps/users/providers/ocs/views.py`

Depending on Task 0: if OCS only shows the team picker on first consent, add `prompt=consent` (or the equivalent OCS parameter) to the authorize URL so a second "Connect another team" click re-opens the picker.

- [ ] **Step 1: Add authorize-URL override**

```python
class OCSOAuth2Adapter(OAuth2Adapter):
    ...
    def get_authorize_url_params(self, request, app):
        params = super().get_authorize_url_params(request, app) if hasattr(super(), "get_authorize_url_params") else {}
        params["prompt"] = "consent"
        return params
```

*(If allauth exposes no such hook, subclass `OAuth2LoginView` and override `get_authorization_url`.)*

- [ ] **Step 2: Manual verification**

Run the dev server, click the "Connect another team" link twice, confirm OCS re-shows its team picker on the second click.

- [ ] **Step 3: Commit**

```bash
git add apps/users/providers/ocs/views.py
git commit -m "feat(ocs-oauth): force consent prompt so OCS re-shows team picker"
```

---

## Task 9: Per-team disconnect + per-team status

**Files:**
- Modify: `apps/users/auth_views.py` — `disconnect_provider_view`, `providers_view`
- Test: extend `tests/test_auth.py` (or add `tests/test_ocs_multi_team_endpoints.py`)

- [ ] **Step 1: Test — listing returns per-team status for OCS**

```python
def test_providers_view_lists_per_team_status_for_ocs(client, user, ocs_social_app):
    ...
    # Expect providers payload to contain: {"id": "ocs", "teams": [
    #   {"team_external_id": "team-alpha", "team_name": "Alpha", "status": "connected"},
    #   {"team_external_id": "team-beta",  "team_name": "Beta",  "status": "expired"},
    # ]}
```

- [ ] **Step 2: Test — disconnect with team param removes only that team's token**

- [ ] **Step 3: Implement**

In `providers_view`, when `app.provider == "ocs"`, populate `entry["teams"]` by listing `OCSTeamToken` rows for the user and running `token_needs_refresh` per row.

In `disconnect_provider_view`, when `provider_id == "ocs"` and the request includes a `team_external_id` (query param or JSON body), delete only that `OCSTeamToken`. With no team_external_id, delete all `OCSTeamToken` rows for the user (full disconnect from OCS).

- [ ] **Step 4: Commit**

```bash
git add apps/users/auth_views.py tests/...
git commit -m "feat(api): per-team status + disconnect for OCS connections"
```

---

## Task 10: Frontend — list connected teams + "Connect another team" button

**Files:**
- Modify: `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx`

- [ ] **Step 1: Update the OCS row**

When the providers payload contains `teams: [...]` for the OCS entry, render:
- One row per connected team with its `team_name`, status badge, and a **Disconnect** button that POSTs to `/api/auth/disconnect/ocs/?team_external_id=<id>`.
- A **Connect another team** button that links to `/accounts/ocs/login/` (re-enters the OAuth flow; with Task 8 the team picker re-appears).

- [ ] **Step 2: Add `data-testid` attributes**

- `ocs-team-row-<team_external_id>`
- `ocs-team-disconnect-<team_external_id>`
- `ocs-connect-another-team`

- [ ] **Step 3: Manual verification in browser**

Start dev servers (`uv run honcho -f Procfile.dev start`). Log in, connect two OCS teams, confirm both appear with working per-team disconnect.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx
git commit -m "feat(ui): show connected OCS teams with per-team disconnect"
```

---

## Self-Review Notes

- Spec coverage: every open question from the top of this document is resolved before Task 1 (Task 0) or explicitly routed to a downstream task.
- Placeholders: Task 7 Step 1 and Task 9 Steps 1-2 contain deliberate test skeletons that depend on Task 0's findings; mark these as requiring concrete fixtures once Task 0 lands.
- Type consistency: `team_external_id` is used throughout; `OCSTeamToken(user, team_external_id, team_name, access_token, refresh_token, expires_at, app)` matches in model, service, signal, resolver, migration, and endpoint tasks.
