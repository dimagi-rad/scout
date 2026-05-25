# OAuth Email-Domain Restriction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict OAuth sign-ins to users whose verified email matches a per-provider allow-list (default `dimagi.com` for all three providers), enforced at allauth's pre-login hook so blocked users never get a `User`, `SocialAccount`, or tenant-resolution side effects.

**Architecture:** Add a Django setting `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS: dict[str, list[str]]` populated from a JSON env var. Extend the existing `EncryptingSocialAccountAdapter` (at `apps/users/adapters.py`) with `pre_social_login`, which checks the email returned by allauth's OAuth callback against the configured allow-list and raises `ImmediateHttpResponse(redirect("account_login"))` after queueing a `messages.error` for the blocked user.

**Tech Stack:** Django 5, django-allauth, django-environ (`env.json(...)`), pytest, pytest-django.

**Spec:** `docs/superpowers/specs/2026-05-25-oauth-domain-restriction-design.md`

---

### Task 1: Add `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` setting

Add a per-provider allow-list to Django settings, configurable via a JSON env
var, with a default that restricts all three known providers to `dimagi.com`.

**Files:**
- Modify: `config/settings/base.py` (add new setting after the existing `SOCIALACCOUNT_PROVIDERS` block near line 229)
- Test: `tests/test_oauth_domain_restriction.py` (new)

- [ ] **Step 1: Write the failing test for the default setting**

Create `tests/test_oauth_domain_restriction.py`:

```python
"""Tests for OAuth email-domain restriction enforcement and configuration."""

from django.conf import settings


class TestAllowedEmailDomainsSetting:
    """Verify the SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting shape and defaults."""

    def test_setting_exists_and_is_dict(self):
        assert isinstance(settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS, dict)

    def test_default_restricts_three_providers_to_dimagi_com(self):
        expected_providers = {"commcare", "commcare_connect", "ocs"}
        actual = settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS
        assert set(actual.keys()) == expected_providers
        for provider, domains in actual.items():
            assert domains == ["dimagi.com"], f"{provider} default should be ['dimagi.com']"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/test_oauth_domain_restriction.py -v`

Expected: FAIL with `AttributeError: ... has no attribute 'SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS'` (or similar).

- [ ] **Step 3: Add the setting**

Open `config/settings/base.py`. Locate the closing brace of `SOCIALACCOUNT_PROVIDERS = {...}` (around line 229) and add the new setting immediately after it:

```python
# OAuth email-domain restriction.
# Map of allauth provider id -> list of allowed email domains (lowercase, exact match).
# A provider absent from the dict (or mapped to an empty list) is unrestricted.
# A provider with a non-empty list rejects OAuth logins whose email domain isn't in the list.
# A login that returns no email is allowed regardless (best-effort: covers providers like
# Connect that don't return an email).
# Override at deploy time with the SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS env var (JSON).
SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS: dict[str, list[str]] = env.json(
    "SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS",
    default={
        "commcare": ["dimagi.com"],
        "commcare_connect": ["dimagi.com"],
        "ocs": ["dimagi.com"],
    },
)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `uv run pytest tests/test_oauth_domain_restriction.py -v`

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add config/settings/base.py tests/test_oauth_domain_restriction.py
git commit -m "feat(auth): add SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting

Per-provider allow-list (provider id -> list of allowed email domains)
parsed from JSON env var, defaulting to ['dimagi.com'] for the three
configured providers."
```

---

### Task 2: Enforce the restriction in `EncryptingSocialAccountAdapter.pre_social_login`

Implement the enforcement: when allauth invokes the adapter's
`pre_social_login` hook after a successful OAuth callback, check the
returned email against the per-provider allow-list and reject mismatches
with a flash message + redirect to login.

**Files:**
- Modify: `apps/users/adapters.py:21-68` (extend `EncryptingSocialAccountAdapter`)
- Modify: `tests/test_oauth_domain_restriction.py` (add enforcement tests)

- [ ] **Step 1: Write the failing tests for enforcement**

Append to `tests/test_oauth_domain_restriction.py`:

```python
from unittest.mock import MagicMock

import pytest
from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, override_settings

from apps.users.adapters import EncryptingSocialAccountAdapter
from apps.users.models import User


def _make_request():
    """Build a request with the messages framework wired in."""
    request = RequestFactory().get("/accounts/commcare/login/callback/")
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _make_sociallogin(provider: str, email: str) -> SocialLogin:
    """Build an in-memory SocialLogin for adapter testing (no DB writes)."""
    user = User(email=email)
    account = SocialAccount(provider=provider, uid="test-uid")
    sociallogin = SocialLogin(user=user, account=account)
    return sociallogin


class TestPreSocialLoginEnforcement:
    """Test the adapter's pre_social_login domain-restriction logic."""

    @pytest.fixture
    def adapter(self):
        return EncryptingSocialAccountAdapter()

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"]})
    def test_allowed_domain_passes(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare", "alice@dimagi.com")
        # Should not raise.
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"]})
    def test_disallowed_domain_blocked(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare", "alice@example.com")
        with pytest.raises(ImmediateHttpResponse) as exc_info:
            adapter.pre_social_login(request, sociallogin)
        response = exc_info.value.response
        assert response.status_code == 302
        assert response.url == "/accounts/login/"
        # An error message should be queued for the user.
        messages = list(request._messages)
        assert len(messages) == 1
        assert "not permitted" in messages[0].message.lower()
        assert "@dimagi.com" in messages[0].message

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"]})
    def test_empty_email_allowed(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare", "")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={})
    def test_unrestricted_provider_allowed(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare_connect", "user@anything.com")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"]})
    def test_provider_not_in_allowlist_is_unrestricted(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("ocs", "user@example.com")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"]})
    def test_case_insensitive_match(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare", "Alice@DIMAGI.COM")
        assert adapter.pre_social_login(request, sociallogin) is None

    @override_settings(
        SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com", "dimagi.org"]}
    )
    def test_multiple_allowed_domains(self, adapter):
        request = _make_request()
        # First domain matches.
        assert adapter.pre_social_login(_make_request(), _make_sociallogin("commcare", "a@dimagi.com")) is None
        # Second domain matches.
        assert adapter.pre_social_login(_make_request(), _make_sociallogin("commcare", "b@dimagi.org")) is None
        # Third domain blocked.
        with pytest.raises(ImmediateHttpResponse):
            adapter.pre_social_login(_make_request(), _make_sociallogin("commcare", "c@other.com"))

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": []})
    def test_empty_allow_list_means_unrestricted(self, adapter):
        request = _make_request()
        sociallogin = _make_sociallogin("commcare", "user@anything.com")
        assert adapter.pre_social_login(request, sociallogin) is None
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/test_oauth_domain_restriction.py::TestPreSocialLoginEnforcement -v`

Expected: every test in `TestPreSocialLoginEnforcement` FAILS. The most likely failure modes:
- `AttributeError: 'EncryptingSocialAccountAdapter' object has no attribute 'pre_social_login'` — except `pre_social_login` exists on the parent `DefaultSocialAccountAdapter` as a no-op, so it will probably **silently pass the "allowed" tests and fail the "blocked" ones** (no `ImmediateHttpResponse` raised). Confirm at least `test_disallowed_domain_blocked` and `test_multiple_allowed_domains` FAIL.

- [ ] **Step 3: Implement `pre_social_login` on the adapter**

Open `apps/users/adapters.py`. Add new module-level imports at the top of the imports block (after the existing `from django.conf import settings`):

```python
from allauth.exceptions import ImmediateHttpResponse
from django.contrib import messages
from django.shortcuts import redirect
```

Then add a new method to `EncryptingSocialAccountAdapter` (place it directly under the class's existing methods, e.g. after `deserialize_instance`):

```python
def pre_social_login(self, request, sociallogin):
    """Reject OAuth logins whose email is not in the per-provider allow-list.

    Runs after a successful OAuth callback but before any User/SocialAccount
    is created or login session established. Configured by the
    SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting (provider id -> list of
    allowed email domains). A provider with no entry (or an empty list) is
    unrestricted; a login that returns no email is allowed regardless.
    """
    provider = sociallogin.account.provider
    allowed = settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS.get(provider) or []
    if not allowed:
        return

    email = (sociallogin.user.email or "").strip().lower()
    if not email:
        return

    domain = email.rpartition("@")[2]
    allowed_lower = [d.lower() for d in allowed]
    if domain in allowed_lower:
        return

    messages.error(
        request,
        "Sign-in with this account is not permitted. "
        f"Only {', '.join('@' + d for d in allowed_lower)} addresses can use {provider}.",
    )
    raise ImmediateHttpResponse(redirect("account_login"))
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `uv run pytest tests/test_oauth_domain_restriction.py -v`

Expected: all 10 tests PASS (2 from Task 1 + 8 from Task 2).

- [ ] **Step 5: Run the existing OAuth-token tests to verify no regression**

Run: `uv run pytest tests/test_oauth_tokens.py -v`

Expected: all existing tests PASS (the new `pre_social_login` method must not affect token encryption/decryption behavior).

- [ ] **Step 6: Commit**

```bash
git add apps/users/adapters.py tests/test_oauth_domain_restriction.py
git commit -m "feat(auth): block OAuth logins with disallowed email domains

EncryptingSocialAccountAdapter.pre_social_login consults the
SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS setting. Logins from providers in the
allow-list whose email domain doesn't match are redirected to the login
page with a flash message; logins with no email or from unrestricted
providers pass through unchanged."
```

---

### Task 3: Document the new env var in `.env.example`

Add a commented sample line so deployments know how to override the default
allow-list.

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Locate the OAuth env block in `.env.example`**

Run: `grep -n "OAUTH\|SOCIAL" .env.example`

Expected: one or more lines covering existing OAuth credentials (e.g.
`GOOGLE_OAUTH_CLIENT_ID`, `COMMCARE_OAUTH_CLIENT_ID`). Note the line range
so the new entry can be placed alongside them.

- [ ] **Step 2: Add the documentation block**

Append to `.env.example` (place it near the other OAuth entries; if there is no obvious block, add it at the end of the file):

```
# Optional: per-provider allow-list of email domains for OAuth sign-in.
# JSON object keyed by allauth provider id ("commcare",
# "commcare_connect", "ocs"); each value is a list of lowercase domains.
# A provider absent from the dict (or with an empty list) is unrestricted.
# A login that returns no email bypasses the check.
# Defaults to ["dimagi.com"] for each of commcare, commcare_connect, and ocs if unset.
# SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"], "commcare_connect": ["dimagi.com"], "ocs": ["dimagi.com"]}
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs: document SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS env var"
```

---

## Self-review notes

- **Spec coverage:** Task 1 covers the setting + default; Task 2 covers every case in the spec's testing section (allowed, blocked, empty-email, unrestricted provider, case-insensitive, multiple domains) plus two additional checks (provider not in allow-list, empty allow-list = unrestricted) that match documented behavior; Task 3 covers the `.env.example` documentation requirement.
- **Placeholders:** none.
- **Type consistency:** `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS: dict[str, list[str]]` used consistently in setting, in `pre_social_login` lookup, and in test fixtures.
- **One subtlety:** `pre_social_login` exists on the parent `DefaultSocialAccountAdapter` as a no-op. The Task 2 Step 2 expectation calls this out so the engineer knows to expect partial test-failure (blocked-case tests fail loudly; allowed-case tests pass-by-accident) rather than the typical "all tests fail" pattern.
