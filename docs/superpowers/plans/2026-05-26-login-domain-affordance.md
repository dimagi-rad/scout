# Login Page Domain-Restriction Affordance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the OAuth email-domain allow-list on the login page so users see the restriction before they click an OAuth button.

**Architecture:** Extend `GET /api/auth/providers/` with per-provider `allowed_email_domains` sourced from `settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`. The React `LoginForm` computes a union across providers and renders a muted line inside the existing "or continue with" divider.

**Tech Stack:** Django 5 (sync view), pytest + Django's test client, React 19 + TypeScript, Tailwind.

**Spec:** [docs/superpowers/specs/2026-05-26-login-domain-affordance-design.md](../specs/2026-05-26-login-domain-affordance-design.md)

---

## Files

- Modify: `apps/users/auth_views.py` (`providers_view`, lines 233-240) — add `allowed_email_domains` to each entry.
- Modify: `tests/test_auth.py` (`TestProvidersEndpoint`, lines 655-688) — two tests asserting the new field.
- Modify: `frontend/src/components/LoginForm/LoginForm.tsx` — extend `OAuthProvider`, compute union, render line in divider.

No new files. No DB migrations.

---

### Task 1: Backend — expose `allowed_email_domains` per provider

**Files:**
- Modify: `apps/users/auth_views.py:233-240`
- Test: `tests/test_auth.py` (`TestProvidersEndpoint`)

- [ ] **Step 1: Add a failing test for the field's presence**

Open `tests/test_auth.py`. In the existing `test_returns_configured_providers` method (around line 659) add an assertion in the loop. Replace the existing test body with:

```python
def test_returns_configured_providers(self, client, google_social_app, github_social_app):
    """Unauthenticated request returns configured providers without connection status."""
    resp = client.get("/api/auth/providers/")
    assert resp.status_code == 200
    data = resp.json()
    assert "providers" in data
    ids = {p["id"] for p in data["providers"]}
    assert "google" in ids
    assert "github" in ids
    for p in data["providers"]:
        assert "name" in p
        assert "login_url" in p
        assert "allowed_email_domains" in p
        assert isinstance(p["allowed_email_domains"], list)
        assert "connected" not in p  # not authenticated
```

- [ ] **Step 2: Add a failing test for the field's value flow**

Append a new test method to `TestProvidersEndpoint` (right after `test_includes_connection_status_when_authenticated`):

```python
def test_includes_allowed_email_domains_from_settings(
    self, client, google_social_app, github_social_app, settings
):
    """allowed_email_domains reflects SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS, defaulting to []."""
    settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS = {"google": ["dimagi.com", "example.org"]}
    resp = client.get("/api/auth/providers/")
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()["providers"]}
    assert by_id["google"]["allowed_email_domains"] == ["dimagi.com", "example.org"]
    assert by_id["github"]["allowed_email_domains"] == []
```

(`settings` is the pytest-django fixture — already in scope across this file.)

- [ ] **Step 3: Run both tests and verify they fail**

```bash
uv run pytest tests/test_auth.py::TestProvidersEndpoint -v
```

Expected: `test_returns_configured_providers` fails on the `"allowed_email_domains" in p` assertion; `test_includes_allowed_email_domains_from_settings` fails with `KeyError` or assertion error on the missing key.

- [ ] **Step 4: Implement — add the field in `providers_view`**

In `apps/users/auth_views.py`, modify the entry dict inside `providers_view` (lines 234-240). Add a new key sourced from settings. Also add the `settings` import if needed (it already exists via `django.conf` elsewhere in the file — check; if not, add `from django.conf import settings` at the top alongside the other Django imports).

Resulting block:

```python
    providers = []
    for app in apps:
        entry = {
            "id": app.provider,
            "name": app.name,
            # No prefix — the frontend prepends BASE_PATH to all API-provided URLs
            "login_url": f"/accounts/{app.provider}/login/",
            "allowed_email_domains": settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS.get(
                app.provider, []
            ),
        }
```

Verify the import: `grep -n "from django.conf" apps/users/auth_views.py`. If `settings` is not imported, add `from django.conf import settings` next to the other `django.*` imports at the top of the file.

- [ ] **Step 5: Run the tests and verify they pass**

```bash
uv run pytest tests/test_auth.py::TestProvidersEndpoint -v
```

Expected: all four `TestProvidersEndpoint` tests pass.

- [ ] **Step 6: Run lint**

```bash
uv run ruff check apps/users/auth_views.py tests/test_auth.py
uv run ruff format --check apps/users/auth_views.py tests/test_auth.py
```

Expected: no errors. If `format --check` fails, run `uv run ruff format apps/users/auth_views.py tests/test_auth.py` and re-verify tests still pass.

- [ ] **Step 7: Commit**

```bash
git add apps/users/auth_views.py tests/test_auth.py
git commit -m "feat(auth): expose allowed_email_domains in providers API"
```

---

### Task 2: Frontend — render the affordance in the divider

**Files:**
- Modify: `frontend/src/components/LoginForm/LoginForm.tsx`

No tests — `LoginForm` has none today, and the codebase doesn't have a React testing setup wired up (only ESLint + tsc). Visual verification by running the dev server is sufficient.

- [ ] **Step 1: Extend the `OAuthProvider` type**

In `frontend/src/components/LoginForm/LoginForm.tsx`, find the interface declaration (lines 11-15) and add the new field:

```ts
interface OAuthProvider {
  id: string
  name: string
  login_url: string
  allowed_email_domains: string[]
}
```

- [ ] **Step 2: Compute the union and render it in the divider**

Inside the `LoginForm` component, just before the `return (` statement, add the union computation:

```tsx
  const restrictedDomains = Array.from(
    new Set(providers.flatMap((p) => p.allowed_email_domains))
  ).sort()
```

Then update the existing divider block (currently lines 93-102) to render a second line when `restrictedDomains.length > 0`. Replace the block with:

```tsx
              <div className="relative my-4">
                <div className="absolute inset-0 flex items-center">
                  <span className="w-full border-t" />
                </div>
                <div className="relative flex flex-col items-center gap-1">
                  <span className="bg-card px-2 text-xs uppercase text-muted-foreground">
                    or continue with
                  </span>
                  {restrictedDomains.length > 0 && (
                    <span
                      className="bg-card px-2 text-xs text-muted-foreground"
                      data-testid="oauth-allowed-domains"
                    >
                      ({restrictedDomains.map((d) => `@${d}`).join(", ")} addresses only)
                    </span>
                  )}
                </div>
              </div>
```

Notes:
- The new `<span>` omits `uppercase` so domains read naturally.
- The wrapper around the spans switched from `flex justify-center` to `flex flex-col items-center gap-1` so the optional second line stacks below.
- `data-testid="oauth-allowed-domains"` follows the project's `{component}-{element}` convention (see CLAUDE.md "data-testid attributes").

- [ ] **Step 3: Type-check the frontend**

```bash
cd frontend && bun run build
```

Expected: build succeeds (runs `tsc` first). If `tsc` flags missing field on `OAuthProvider` usages elsewhere, fix them — though a `grep -rn "OAuthProvider" frontend/src` should show this interface is local to `LoginForm.tsx` only.

- [ ] **Step 4: Lint**

```bash
cd frontend && bun run lint
```

Expected: no new errors in `LoginForm.tsx`.

- [ ] **Step 5: Visual smoke test**

Start the dev servers and verify in a browser:

```bash
uv run honcho -f Procfile.dev start
```

Then in another terminal, ensure a SocialApp exists with `provider="commcare"` (the default `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` setting already restricts `commcare` to `dimagi.com`). If no SocialApp is configured locally, run the setup command — check `apps/users/management/commands/` for one (likely `setup_oauth_apps`):

```bash
uv run python manage.py setup_oauth_apps --help
```

Open `http://localhost:5173/` (Vite dev), log out if needed, and confirm the login card shows:

```
─────── or continue with ───────
        (@dimagi.com addresses only)
```

Then quickly verify the empty case by overriding the setting in `.env.dev` or temporarily editing `config/settings/development.py` to set `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS = {}`, restart the Django server, and confirm the second line is absent. Revert the temporary change before committing.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/LoginForm/LoginForm.tsx
git commit -m "feat(login): show allowed email domains in OAuth divider"
```

---

### Task 3: Wrap up — verify and PR

- [ ] **Step 1: Full backend test pass**

```bash
uv run pytest tests/test_auth.py -v
```

Expected: all pass. No collateral failures.

- [ ] **Step 2: Lint sweep**

```bash
uv run ruff check .
cd frontend && bun run lint
```

Expected: clean.

- [ ] **Step 3: Push and open draft PR**

```bash
git push -u origin sk/login-domain-affordance
```

Use the `dev-utils:create-pr` skill (or `gh pr create --draft`) with a short description pointing to the spec.

---

## Notes for the implementer

- `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` lives in `config/settings/base.py:229`. The default already restricts `commcare`, `commcare_connect`, and `ocs` to `["dimagi.com"]`, so in a default dev environment the new line will render automatically once OAuth apps are configured.
- The setting is keyed by allauth **provider class id**, which is the same value as `SocialApp.provider`. No mapping needed.
- An empty allow-list (or absent key) means unrestricted — the field returns `[]`, and the union skips it naturally via `flatMap`.
- Do not move the domain list to per-button rendering. The spec explicitly defers that until divergence becomes real.
