# Login page domain-restriction affordance

## Goal

Tell users which email domains are allowed for OAuth sign-in **before** they
click an OAuth button, so they don't bounce off `pre_social_login` and read the
restriction in an error toast.

## Motivation

The OAuth allow-list ([2026-05-25-oauth-domain-restriction-design.md](2026-05-25-oauth-domain-restriction-design.md))
silently rejects sign-ins whose email domain isn't in
`SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`. The current UX is invisible until a user
has already completed the OAuth round-trip. We want the restriction visible on
the login card itself.

Scope: OAuth providers only. Email/password login has no domain gate today.

## Design

### Backend — `apps/users/auth_views.py:providers_view`

Each entry returned from `GET /api/auth/providers/` gains:

```python
"allowed_email_domains": settings.SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS.get(app.provider, []),
```

Empty list = unrestricted. The frontend computes a union across all entries
for display.

### Frontend — `frontend/src/components/LoginForm/LoginForm.tsx`

Extend `OAuthProvider`:

```ts
interface OAuthProvider {
  id: string
  name: string
  login_url: string
  allowed_email_domains: string[]
}
```

Compute once from the providers list:

```ts
const restrictedDomains = Array.from(
  new Set(providers.flatMap(p => p.allowed_email_domains))
).sort()
```

In the existing "or continue with" divider (currently lines 93-102), render a
second muted line below when `restrictedDomains.length > 0`:

- 1 domain  → `(@dimagi.com addresses only)`
- N domains → `(@dimagi.com, @example.org addresses only)`

Styling matches the existing divider caption (`text-xs uppercase text-muted-foreground`),
without the uppercase modifier on the domain line so the email reads naturally.

### Visual

The chosen layout (option C from brainstorming): the affordance lives inline
with the divider, immediately above the OAuth buttons. No per-button decoration.

## Tests

`tests/test_auth.py::TestProvidersEndpoint`:

- Add an assertion that each entry exposes `allowed_email_domains`.
- Add a case under `@override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={"commcare": ["dimagi.com"]})`
  that verifies the value flows through.

No frontend tests — login form has none today, and the change is a
straightforward template branch.

## Out of scope

- Per-button rendering when allow-lists diverge across providers. Today they
  all share `dimagi.com`; revisit if divergence becomes real.
- Restricting the email/password form. That has no backend gate to mirror.
- Wording polish (`only` vs `required`, parenthetical vs not). Easy to iterate
  in review.
