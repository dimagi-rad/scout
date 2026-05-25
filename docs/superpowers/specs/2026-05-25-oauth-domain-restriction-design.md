# OAuth email-domain restriction

## Goal

Restrict OAuth sign-ins to users whose verified email matches an allow-list of
domains (e.g. `dimagi.com`), configurable per OAuth provider. Lock the policy
in at the earliest enforcement point so blocked users never get a Django
`User` row, `SocialAccount`, or tenant resolution side-effects.

## Motivation

Scout currently accepts any successful OAuth sign-in from CommCare HQ,
CommCare Connect, and OCS, and auto-creates a Django user on first login.
For deployments that should only serve Dimagi staff, we want a hard
restriction on the email domain returned by the provider.

The restriction needs to be configurable per provider because the providers
have different semantics:

- CommCare HQ may or may not return an email depending on scopes.
- CommCare Connect does not return an email at all.
- OCS varies by deployment.

## Configuration

A single Django setting:

```python
# config/settings/base.py
SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS: dict[str, list[str]] = env.json(
    "SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS",
    default={
        "commcare": ["dimagi.com"],
        "commcare_connect": ["dimagi.com"],
        "ocs": ["dimagi.com"],
    },
)
```

- Keys are allauth provider ids (`sociallogin.account.provider`).
- Values are lowercase email domains, exact-match (no subdomain wildcards).
- A provider key absent from the dict, or mapped to an empty list, is
  **unrestricted** — any email (or no email) is allowed.
- A provider key mapped to a non-empty list is **restricted**: a returned
  email must end with `@<one of the domains>` (case-insensitive). A missing
  email is still allowed (best-effort policy — covers providers like Connect
  that don't return an email).

Deployments override by setting the `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`
environment variable to a JSON object with the same shape. `.env.example`
gets a commented sample line showing the JSON shape.

## Enforcement

Extend the existing `EncryptingSocialAccountAdapter` in
`apps/users/adapters.py` with a `pre_social_login` method.

`pre_social_login` runs after the OAuth callback succeeds but before any
`User`, `SocialAccount`, or login session is created. It also runs before
the `social_account_added` signal that triggers
`resolve_tenant_on_social_login`, so blocked users never reach tenant
resolution.

```python
from allauth.exceptions import ImmediateHttpResponse
from django.contrib import messages
from django.shortcuts import redirect

class EncryptingSocialAccountAdapter(DefaultSocialAccountAdapter):
    ...

    def pre_social_login(self, request, sociallogin):
        provider = sociallogin.account.provider
        allowed = settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS.get(provider, [])
        if not allowed:
            return  # provider unrestricted

        email = (sociallogin.user.email or "").strip().lower()
        if not email:
            return  # no email returned — allow

        domain = email.rpartition("@")[2]
        if domain in (d.lower() for d in allowed):
            return  # match — allow

        messages.error(
            request,
            f"Sign-in with this account is not permitted. "
            f"Only {', '.join('@' + d for d in allowed)} addresses can use {provider}.",
        )
        raise ImmediateHttpResponse(redirect("account_login"))
```

Allauth catches `ImmediateHttpResponse` and returns the embedded response
verbatim, so the redirect lands the user back at `/accounts/login/` with the
flash message rendered by Django's `messages` framework.

The restriction applies to **every** OAuth login attempt — new signups and
existing users re-authenticating. Existing accounts with non-allowed emails
that were created before the policy took effect will be locked out of OAuth
login (they can still sign in with a password if one was set).

## Testing

New file `tests/test_oauth_domain_restriction.py`. Tests construct
`SocialLogin` and `SocialAccount` objects in-memory (no real OAuth
roundtrip), use a `RequestFactory`-built request with the `messages`
middleware shim, and `override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS=...)`.

Cases:

1. **Allowed domain passes** — `provider="commcare"`, email
   `"alice@dimagi.com"`, settings `{"commcare": ["dimagi.com"]}`.
   `pre_social_login` returns `None`; no exception raised.
2. **Disallowed domain blocked** — same setup, email
   `"alice@example.com"`. Asserts `ImmediateHttpResponse` raised; embedded
   response is a redirect to `account_login`; a `messages.error` is queued.
3. **Empty email allowed** — `sociallogin.user.email = ""`, restriction
   configured. Returns `None`.
4. **Unrestricted provider allowed** — `provider="commcare_connect"`,
   settings `{}`. Returns `None` regardless of email.
5. **Case-insensitive match** — email `"Alice@DIMAGI.COM"`, allow-list
   `["dimagi.com"]`. Allowed.
6. **Multiple domains** — `{"commcare": ["dimagi.com", "dimagi.org"]}`.
   Emails on both domains pass; an email on a third domain is blocked.

## Out of scope

- Subdomain wildcards (`*.dimagi.com`).
- Per-`SocialApp`-row admin-editable overrides (env-only configuration).
- Restrictions on password-based signin (untouched — only OAuth flows are
  affected).
- Proactive cleanup of existing accounts with non-allowed emails (policy
  applies on next OAuth login; no migration walks existing users).
