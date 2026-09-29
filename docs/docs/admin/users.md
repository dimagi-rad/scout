# Users

Scout uses Django's authentication with a custom user model that identifies users by email. Accounts created through CommCare Connect may have no email, because Connect does not always provide one.

What a user can do is set per workspace by their workspace role. See [Workspaces](workspaces.md#roles).

## Logging in

### Email and password

The login page accepts an email and password (`POST /api/auth/login/`), which creates a session cookie.

After 5 failed attempts for an email address, further attempts for that address are refused for 5 minutes.

### OAuth providers

Scout supports OAuth login through django-allauth with these providers:

| Provider | ID | Notes |
|----------|----|-------|
| CommCare HQ | `commcare` | Logging in also discovers your project spaces as data sources. Restricted to `dimagi.com` emails by default. |
| CommCare Connect | `commcare_connect` | Logging in also discovers your opportunities as data sources. |
| Open Chat Studio | `ocs` | Logging in also discovers your chatbots as data sources. You can connect more than one team. |
| Google | `google` | Login only; no data sources. |
| GitHub | `github` | Login only; no data sources. |

The login page shows a button for each provider that has an OAuth app configured. To configure them, set `<PREFIX>_OAUTH_CLIENT_ID` and `<PREFIX>_OAUTH_CLIENT_SECRET` (prefixes `COMMCARE`, `CONNECT`, `OCS`, `GOOGLE`, `GITHUB`) and run:

```bash
uv run manage.py setup_oauth_apps --domain scout.example.com
```

The command is idempotent, so re-run it after rotating credentials. You can also edit the apps in the Django admin at `/admin/socialaccount/socialapp/`.

To change which email domains may log in with a provider, set `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` to a JSON object mapping provider IDs to domain lists, e.g. `{"commcare": ["dimagi.com"]}`. A provider with no entry accepts any email.

The first OAuth login creates a Scout user. If a user with the same email already exists and has proven ownership of it (a verified email, or a trusted provider account that asserted it), the new login is linked to that user.

### Connected Accounts

The **Connected Accounts** page (in the sidebar) lists your OAuth connections and lets you connect or disconnect them. **Add API Connection** connects CommCare HQ or Open Chat Studio with an API key instead of OAuth. Scout uses these credentials to load data from your data sources.

## Sessions and endpoints

Scout uses session cookies, not JWTs. The frontend gets a CSRF token from `/api/auth/csrf/` and sends it with API requests.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/csrf/` | GET | Set the CSRF cookie |
| `/api/auth/me/` | GET | Current user info |
| `/api/auth/login/` | POST | Email/password login |
| `/api/auth/logout/` | POST | End the session |
| `/api/auth/providers/` | GET | OAuth providers and your connection status |

## Creating users

Users are created by:

1. **OAuth login.** The first login with any configured provider creates the user.
2. **`createsuperuser`.** Run `uv run manage.py createsuperuser`.
3. **The Django admin** at `/admin/users/user/add/`.

There is no self-service sign-up form in the UI. To give someone access to a workspace, a workspace manager adds them by email (see [Workspaces](workspaces.md#members)). If they don't have an account yet, the invite resolves when they first log in.

## Superusers

Django superusers can use the Django admin at `/admin/` to manage users, workspaces, and other records. Create the first one with `uv run manage.py createsuperuser`.

## Duplicate accounts

If duplicate users with the same email exist, merge them with:

```bash
uv run manage.py merge_duplicate_users --dry-run   # preview
uv run manage.py merge_duplicate_users
```
