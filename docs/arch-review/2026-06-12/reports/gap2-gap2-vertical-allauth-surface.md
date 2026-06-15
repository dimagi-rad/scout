# Gap-2 Vertical: the live `/accounts/` (allauth) auth surface

Reviewer mandate: enumerate every live URL under `/accounts/` (stock `allauth.urls`
mounted wholesale at `config/urls.py:83`) and audit each against the app's own auth
policies — the API-side signup flow (`apps/users/auth_views.py`),
`apps/users/rate_limiting.py`, and the email-domain allowlist
(`SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS`).

REPORT ONLY. No code changed. All URL enumeration done by dumping the resolved
URLconf via `django.setup()` + `get_resolver()`, not by reading `allauth` source for
route names.

---

## The surface that is actually mounted

`config/urls.py:83`:

```python
path("accounts/", include("allauth.urls")),
```

Resolved URLconf (34 patterns; `manage.py`-equivalent dump under
`config.settings.development`, allauth **65.14.0**, no `allauth.headless` installed —
this is the **HTML** surface). The interesting, policy-relevant ones:

| URL | name | method | what it does |
|---|---|---|---|
| `accounts/signup/` | `account_signup` | GET/POST | **self-service email+password registration** |
| `accounts/login/` | `account_login` | GET/POST | email+password login form |
| `accounts/logout/` | `account_logout` | POST only | logout (`ACCOUNT_LOGOUT_ON_GET=False`) |
| `accounts/password/reset/` | `account_reset_password` | GET/POST | request password-reset email |
| `accounts/password/reset/key/<uidb36>-<key>/` | `account_reset_password_from_key` | GET/POST | set new password from email link |
| `accounts/password/change/` | `account_change_password` | GET/POST | change password (auth required) |
| `accounts/password/set/` | `account_set_password` | GET/POST | set password (auth required) |
| `accounts/email/` | `account_email` | GET/POST | add/remove/verify email addresses (auth required) |
| `accounts/confirm-email/<key>/` | `account_confirm_email` | GET/POST | verify an email address |
| `accounts/reauthenticate/` | `account_reauthenticate` | GET/POST | step-up reauth |
| `accounts/<provider>/login/` | `<provider>_login` | **GET** | initiate OAuth (`SOCIALACCOUNT_LOGIN_ON_GET=True`) |
| `accounts/<provider>/login/callback/` | `<provider>_callback` | GET | OAuth callback |
| `accounts/3rdparty/` | `socialaccount_connections` | GET/POST | manage connected social accounts |
| `accounts/3rdparty/signup/` | `socialaccount_signup` | GET/POST | finalize a social signup |

Providers wired: `google`, `github`, `commcare`, `commcare_connect`, `ocs`
(plus `google_login_by_token`).

**All of these render in production.** Verified the template loader resolves the
stock allauth pages against the repo's custom layout:

```
account/login.html          -> allauth/templates/account/login.html (package default)
account/signup.html         -> allauth/templates/account/signup.html (package default)
account/password_reset.html -> allauth/templates/account/password_reset.html (package default)
account/email.html          -> allauth/templates/account/email.html (package default)
allauth/layouts/base.html   -> templates/allauth/layouts/base.html (REPO OVERRIDE, Scout-branded)
```

So `/accounts/signup/`, `/accounts/login/`, `/accounts/password/reset/`, and the email
management page are **fully functional HTML forms** in production, styled with the
Scout brand — not 404s, not unstyled. The SPA is the *intended* UI, but the allauth
HTML UI is live and reachable, and the SPA links into it directly:

- `frontend/src/components/OnboardingWizard/OnboardingWizard.tsx:127` → `/accounts/commcare/login/?next=/`
- `frontend/src/components/LoginForm/LoginForm.tsx:113` → `${provider.login_url}` (= `/accounts/<provider>/login/`, from `providers_view` `auth_views.py:255`)
- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx:235` → `/accounts/<provider>/login/?process=connect&next=/settings/connections`

---

## Findings

### F1 — `/accounts/signup/` is a second, divergent, open registration surface that bypasses `signup_view` (LATENT / security+correctness)

**Confidence: verified-by-trace.**

There are now **two** self-service email+password registration endpoints, and they
produce **different account state**:

- `POST /api/auth/signup/` → `apps/users/auth_views.py:137` `signup_view`. Calls
  `UserModel.objects.create_user(email=..., password=...)` (`auth_views.py:165`) and
  logs in. It creates **no `allauth.account.models.EmailAddress` row**.
- `GET/POST /accounts/signup/` → stock allauth `account_signup`. Uses the **default**
  account adapter (`ACCOUNT_ADAPTER` is unset → `allauth.account.adapter`,
  `is_open_for_signup` = `DefaultAccountAdapter.is_open_for_signup` → `True`). It runs
  allauth's own form/password validation and **creates an `EmailAddress` row**
  (`verified=False`, since `ACCOUNT_EMAIL_VERIFICATION='optional'`,
  `config/settings/base.py:199`).

Neither path is gated by the email-domain allowlist (see F6); the allowlist only fires
in `EncryptingSocialAccountAdapter.pre_social_login` (`adapters.py:74`), i.e. social
logins only. So `/accounts/signup/` lets anyone on the internet self-register a local
account with any email domain, identically to `/api/auth/signup/` — but via a UI the
SPA never shows and with different downstream state (an `EmailAddress` row that
`signup_view` never creates). Whatever invariants the rest of the app assumes from
`_user_response` / `signup_view` (no `EmailAddress`) are violated for accounts born on
the allauth path.

Essential vs accidental: **accidental.** `include("allauth.urls")` mounts the entire
HTML account surface wholesale; the app only needs the social-provider login/callback
routes (the SPA owns login/signup/password). Mounting the whole module is the cause.

Reachable via: `https://<host>/accounts/signup/` directly (no SPA link needed; it is a
public GET).

---

### F2 — Refinement of the known merge-gate finding: the allauth signup path is the *only* way an `EmailAddress` is ever created locally, and it is unverified (LATENT / correctness)

**Confidence: verified-by-trace** for the asymmetry; **strong-inference** for the
practical consequence.

The known finding states the merge gate "requires a verified `EmailAddress` the system
never produces." This vertical refines that:

The merge gate is `reconcile_existing_user_on_login` (`signals.py:104`):

```python
canonical_owns_email = EmailAddress.objects.filter(
    user=canonical, email__iexact=new_email, verified=True,
).exists()
if not canonical_owns_email:
    logger.warning("Refusing auto-merge: ...")
    return
```

- `signup_view` (`/api/auth/signup/`) creates **zero** `EmailAddress` rows → a
  canonical account born here can *never* satisfy `verified=True`. (Matches the known
  finding.)
- `/accounts/signup/` **does** create an `EmailAddress`, but `verified=False`
  (`ACCOUNT_EMAIL_VERIFICATION='optional'` neither auto-verifies nor blocks login). It
  only flips to `verified=True` if the user clicks the verification link in the email
  allauth sends on signup.

So the correct statement is: the merge gate is satisfiable **only** for users who (a)
registered via the hidden `/accounts/signup/` surface AND (b) actually received and
clicked a verification email. Via the SPA's `/api/auth/signup/`, it is never
satisfiable. And in production, (b) is impossible because the email backend doesn't
work (F3). Net behavior is unchanged from the known finding, but the *reason* is the
two-surface asymmetry plus a dead mail backend, not "the system never produces one."

---

### F3 — Production has no working email backend: `/accounts/password/reset/` and email verification cannot deliver (BROKEN-NOW / correctness)

**Confidence: verified-by-trace** (settings resolution) + **strong-inference** (no MTA
on the container).

Neither `config/settings/base.py` nor `config/settings/production.py` sets
`EMAIL_BACKEND`. Resolved under `config.settings.production`:

```
PROD EMAIL_BACKEND: django.core.mail.backends.smtp.EmailBackend   # Django default
PROD EMAIL_HOST:    localhost
PROD EMAIL_PORT:    25
DEFAULT_FROM_EMAIL: webmaster@localhost
```

(Only `development.py:10` and `test.py:41` set a backend — console / locmem.)

The live `/accounts/password/reset/` (`account_reset_password`) and the email
verification flow (`account_confirm_email`, and the signup confirmation send for
`ACCOUNT_EMAIL_VERIFICATION='optional'`) all call `send_mail` against
`smtp://localhost:25`. A Fargate/ECS app container has no local MTA, so every such send
either:

- raises `ConnectionRefusedError` → the reset/verify request **500s**, or
- (if buffered/async in some configs) silently never delivers.

Either way the password-reset surface is non-functional in production. allauth's reset
view, to prevent account enumeration, renders "we've sent you an email" regardless — so
a user who resets a local-account password gets a success page and no email, forever.
This compounds F2: the one path that could mark an `EmailAddress` verified (and thus
unlock the merge gate) is dead because the confirmation email can't be sent.

Reachable via: `https://<host>/accounts/password/reset/` (public GET form).

Essential vs accidental: **accidental** — a missing config line on a surface the team
probably didn't intend to expose.

---

### F4 — The allauth HTML surface bypasses `apps/users/rate_limiting.py`; it has allauth's own limits, but they share the same per-process LocMemCache (LATENT / security)

**Confidence: verified-by-trace.**

`apps/users/rate_limiting.py` (`check_rate_limit` / `record_attempt`, 5 attempts /
300s, keyed by email) is wired **only** into `login_view` (`auth_views.py:109`) and
`signup_view` (`auth_views.py:150`). It is not on the call path of any `/accounts/`
view. So the custom lockout does not protect `/accounts/login/` or `/accounts/signup/`.

allauth 65 does ship active default rate limits (resolved at runtime):

```
login:        30/m/ip
login_failed: 10/m/ip,5/300s/key
signup:       20/m/ip
reset_password: 20/m/ip,5/m/key
reset_password_from_key: 20/m/ip
manage_email: 10/m/user
change_password: 5/m/user
```

So `/accounts/login/` is **not** unthrottled. But two problems:

1. **Two independent, divergent throttles for the same action.** `/api/auth/login/` is
   limited by the custom 5/300s-per-email rule; `/accounts/login/` by allauth's
   10/m/ip + 5/300s/key. An attacker can spread attempts across both surfaces and
   across IPs; the per-email custom counter and the allauth per-key counter live under
   **different cache keys** (`auth_attempts:<email>` vs `allauth/rl/...`) and never
   share a budget.
2. **Both back onto `LocMemCache`** (`config/settings/base.py:318-322`, with the repo's
   own warning comment at lines 324-325 that it "won't work across multiple workers").
   Under N uvicorn workers the effective limit is ~N× the configured value, and every
   deploy resets all counters. This is the known "rate limiting uses per-process
   LocMemCache" weakness, here extended to **both** the custom auth limiter and
   allauth's. (The admin-login bypass is already a separate known finding; this is the
   allauth/auth-API surface.)

Essential vs accidental: **accidental** — Redis is provisioned but unused
(`config/settings/base.py:324-325` comment), so the fix is a `CACHES` change.

---

### F5 — `SOCIALACCOUNT_LOGIN_ON_GET=True` makes every provider login/connect a CSRF-able GET; combined with `EMAIL_AUTHENTICATION_AUTO_CONNECT=True` this is the allauth-documented login-CSRF / forced-link vector (LATENT / security)

**Confidence: strong-inference.**

`config/settings/base.py:204` `SOCIALACCOUNT_LOGIN_ON_GET = True`. Resolved
`LOGIN_ON_GET: True`. Every `/accounts/<provider>/login/` (and the connect variant
`?process=connect`) initiates the OAuth redirect on a **GET with no CSRF token**.
allauth's own docs warn that `LOGIN_ON_GET=True` "may open you up to login CSRF." The
SPA relies on exactly these GET links:

- `ConnectionsPage.tsx:235` → `/accounts/<provider>/login/?process=connect&next=/settings/connections`
- `OnboardingWizard.tsx:127`, `LoginForm.tsx:113` → `/accounts/<provider>/login/?next=...`

Combined with `SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True`
(`base.py:209`, resolved `True`) and `SOCIALACCOUNT_EMAIL_AUTHENTICATION = True`
(`base.py:212`), plus every custom provider marked `VERIFIED_EMAIL: True`
(`base.py:225-238`): a social login whose email matches an existing user is
**auto-connected to that user with no confirmation step**. The classic exploit shapes:

- *Forced login (login CSRF):* an attacker page auto-navigates a victim to
  `/accounts/<provider>/login/`; if the victim has a live provider session, they get
  silently logged into Scout (or into the attacker's pre-linked account), enabling
  session-fixation-style traps.
- *Forced account linking:* a malicious page triggers `?process=connect` against a
  logged-in victim to attach an attacker-controlled provider identity, or vice-versa.

The OAuth `state` parameter does protect the *callback* leg, so this is not a one-click
takeover; the residual risk is the unauthenticated GET *initiation* plus auto-connect.
For Google/GitHub the provider verifies the email so auto-connect targets the user's
real address; the sharper edge is the custom providers (`commcare_connect`, `ocs`) that
are *configured* `VERIFIED_EMAIL=True` but whose actual email-verification guarantees
are exactly the subject of the known OCS-allowlist / merge-gate findings.

Essential vs accidental: **mixed.** `LOGIN_ON_GET=True` is a deliberate UX choice
(plain `<a>` links instead of POST forms in the SPA); the CSRF exposure is the
accidental cost of that choice riding on the wholesale allauth mount.

---

### F6 — `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` restricts only the `commcare` provider; every other registration path is open (LATENT / security)

**Confidence: verified-by-trace** (config). Overlaps the known OCS-allowlist finding;
scoped here to the full registration surface.

Resolved default (`base.py:247-252`): `{"commcare": ["dimagi.com"]}`. Enforcement is
solely in `adapters.py:74-104` `pre_social_login`, which returns early for any provider
not in the dict (`adapters.py:84-86`) and for any login that returns no email
(`adapters.py:88-90`). Consequences for the whole registration surface:

- `google`, `github`, `commcare_connect`, `ocs` social logins: **no domain
  restriction**. With `SOCIALACCOUNT_AUTO_SIGNUP=True` (`base.py:205`, resolved
  `True`), anyone with any Google or GitHub account can auto-create a Scout account.
- `/accounts/signup/` (F1) and `/api/auth/signup/`: **no domain restriction** — any
  email, any domain.

So the "allowed email domains" control is not a registration access control; it gates
exactly one provider's email domain and nothing else. Account-level access is open;
the only real boundary is the tenant/workspace layer (a fresh account sees no data).
That may be intended, but the setting's name and the `commcare: ["dimagi.com"]` default
imply a perimeter that does not exist for five of the six entry points.

Essential vs accidental: **accidental** (naming + scope mismatch).

---

## What's fine (verified healthy)

- **`next=` / redirect validation is safe.** allauth resolves post-login redirects via
  `SocialLogin.get_redirect_url` → adapter `is_safe_url` → Django
  `url_has_allowed_host_and_scheme` against `ALLOWED_HOSTS`. The SPA passes relative
  `next` (`/`, `/settings/connections`), which is host-safe. No open redirect found on
  the allauth views.
- **Domain-allowlist ordering is correct.** In `allauth/socialaccount/internal/flows/login.py:31-37`,
  `get_adapter().pre_social_login(...)` (the domain check, which raises
  `ImmediateHttpResponse`) runs **before** the `pre_social_login` *signal*
  (`reconcile_existing_user_on_login`) and **before** the account is persisted in
  `_authenticate`/`do_connect`. A disallowed-domain login aborts before any merge or
  auto-connect is written. No ordering bug.
- **Logout is POST-only** (`ACCOUNT_LOGOUT_ON_GET=False`), so no logout-CSRF.
- **Login-by-code is off** (`LOGIN_BY_CODE_ENABLED=False`), so the
  `accounts/login/code/confirm/` route is inert.
- **Token encryption works.** `EncryptingSocialAccountAdapter.serialize_instance` /
  `deserialize_instance` (`adapters.py:54-72`) Fernet-wrap `SocialToken.token` /
  `token_secret` at rest, keyed by `DB_CREDENTIAL_KEY`.
- **`allauth.headless` is not installed** — only the HTML surface exists; there is no
  unauthenticated JSON account API to worry about.
- **CSRF protection on the POST forms** is intact (`CsrfViewMiddleware` +
  `AccountMiddleware` both active; the only CSRF-relevant gap is the deliberate
  `LOGIN_ON_GET` GET initiations in F5).

---

## Coverage log

**Deep-read (line-by-line):**
- `config/urls.py` (full)
- `config/settings/base.py` (full — allauth/socialaccount block lines 189-252, caches 318-330)
- `config/settings/production.py` (full — confirmed no `EMAIL_BACKEND` override)
- `apps/users/adapters.py` (full)
- `apps/users/rate_limiting.py` (full)
- `apps/users/auth_views.py` (full)
- `apps/users/signals.py` (full)
- `apps/users/apps.py` (full — signal wiring)
- `templates/allauth/layouts/base.html` (full)
- `.venv/.../allauth/socialaccount/internal/flows/login.py:28-72` (pre_social_login ordering)

**Verified by runtime introspection (not just reading):**
- Full resolved `/accounts/` URLconf (34 patterns) via `get_resolver()`
- allauth runtime settings: `RATE_LIMITS`, `EMAIL_VERIFICATION`, `LOGIN_ON_GET`,
  `EMAIL_AUTHENTICATION_AUTO_CONNECT`, `EMAIL_AUTHENTICATION`, `AUTO_SIGNUP`,
  `LOGOUT_ON_GET`, `LOGIN_BY_CODE_ENABLED`, `is_open_for_signup`
- Template loader resolution for `account/login|signup|password_reset|email.html`
- Production-settings resolution of `EMAIL_BACKEND` / `EMAIL_HOST` / `EMAIL_PORT` / `DEFAULT_FROM_EMAIL`
- allauth version (65.14.0); `allauth.headless` absence

**Skimmed:**
- `config/settings/development.py`, `config/settings/test.py` (email backend lines only)
- `frontend/src/.../OnboardingWizard.tsx`, `LoginForm.tsx`, `ConnectionsPage.tsx`
  (only the lines that link to `/accounts/`)
- `apps/users/providers/` (presence confirmed via INSTALLED_APPS + URLconf; provider
  internals NOT audited)

**NOT examined (gaps for a later round):**
- The custom provider implementations under `apps/users/providers/commcare/`,
  `commcare_connect/`, `ocs/` — their `extract_uid` / `extract_email_addresses` /
  `extract_common_fields` and whether `VERIFIED_EMAIL=True` is honest (directly feeds
  F5's auto-connect risk; deferred to the OAuth-provider vertical).
- `apps/users/services/merge.py` internals (the merge-gate consequence side; known
  finding owns it).
- `apps/users/services/tenant_resolution.py` and `credential_resolver.py` (referenced
  by the post-login signals but out of the allauth-surface scope).
- Whether a reverse proxy / WAF blocks `/accounts/signup/` and `/accounts/password/reset/`
  in the deployed environment (infra-config, not in repo) — F1/F3 assume the routes are
  internet-reachable as mounted.
- The actual SMTP topology in prod (assumed no MTA at localhost:25; not verifiable from
  the repo — F3's deliverability is strong-inference, the missing backend is verified).
- `account/3rdparty/` (`socialaccount_connections`) POST handlers for
  disconnect-via-allauth vs the app's own `disconnect_provider_view`
  (`auth_views.py:177`) — possible divergent disconnect semantics, not traced.
- allauth's `ACCOUNT_PREVENT_ENUMERATION` default and whether login/signup error
  messages leak account existence (only spot-checked the reset page behavior).
