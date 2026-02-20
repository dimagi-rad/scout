# Testing CommCare integration in development

Scout integrates with CommCare HQ to materialize case data for AI-powered querying. This guide covers two ways to authenticate during local development:

- **API key** — the simpler path; no tunneling required
- **OAuth via ngrok** — required if you want to test the full OAuth flow

## Prerequisites

Complete [installation](installation.md) first. You need a running backend (`http://localhost:8000`) and a CommCare HQ account with access to at least one domain.

---

## Option A: API key auth (recommended for dev)

This path lets you authenticate with a CommCare username and API key directly from the onboarding wizard — no OAuth app setup or external tunnel needed.

### 1. Create an API key in CommCare HQ

1. Log in to [CommCare HQ](https://www.commcarehq.org).
2. Go to **Settings → My Account → API Keys**.
3. Create a new key and copy the value. The key is shown once.

The credential format used by Scout is `username@example.com:your-api-key`.

### 2. Sign up for a Scout account

1. Open `http://localhost:5173`.
2. Click **Sign up** and create a local account with email and password.
3. After logging in, the **Onboarding Wizard** appears.

### 3. Configure your CommCare domain in the wizard

In the wizard:

1. Choose **API key** as the authentication method.
2. Enter your CommCare **domain name** (the slug from the URL, e.g. `my-project` from `www.commcarehq.org/a/my-project/`).
3. Enter a **display name** for this domain.
4. Paste your credential in the format `username@example.com:your-api-key`.
5. Click **Save**.

Scout stores the credential encrypted (Fernet, same key as `DB_CREDENTIAL_KEY`) and creates a `TenantMembership` + `TenantCredential` record.

### 4. Load cases and start querying

With the domain configured, open the chat interface and type:

```
Load my cases
```

or

```
Run materialization
```

The agent calls the `run_materialization` MCP tool, which fetches cases from the CommCare Case API v2 and writes them to a PostgreSQL schema named after your domain.

> **Note:** Materialization requires `MANAGED_DATABASE_URL` (see [Managed database setup](#managed-database-setup) below). In development, this defaults to your main app database with per-domain schema isolation, so no extra setup is needed.

---

## Option B: OAuth flow via ngrok

Use this path to test the full CommCare OAuth flow — CommCare redirects back to your local server after authorisation, so it needs a publicly accessible URL.

### 1. Install and start ngrok

```bash
# Install ngrok (https://ngrok.com/download) then:
ngrok http 8000
```

Note the HTTPS forwarding URL — it looks like `https://abc123.ngrok-free.app`.

### 2. Create a CommCare OAuth application

1. In CommCare HQ, go to **Settings → My Account → API Keys → OAuth2 Applications** (or ask your CommCare admin to create one).
2. Create a new **Confidential** application.
3. Set **Redirect URI** to:

   ```
   https://abc123.ngrok-free.app/accounts/commcare/login/callback/
   ```

4. Note the **Client ID** and **Client Secret**.

### 3. Register the OAuth app in Django admin

1. Open `http://localhost:8000/admin/` and log in as a superuser.
2. Go to **Social accounts → Social applications → Add**.
3. Fill in:
   - **Provider**: `CommCare`
   - **Name**: `CommCare HQ` (any label)
   - **Client ID**: your client ID from step 2
   - **Secret key**: your client secret from step 2
   - **Sites**: move `example.com` (or your configured site) to **Chosen sites**
4. Save.

### 4. Update your `.env` for ngrok

Add these lines (replace the URL with your ngrok URL):

```bash
# Needed so Django accepts the ngrok Host header
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.ngrok-free.app

# Needed so CSRF validation passes over HTTPS
CSRF_TRUSTED_ORIGINS=https://abc123.ngrok-free.app

# Needed so allauth generates https:// callback URLs
ACCOUNT_DEFAULT_HTTP_PROTOCOL=https
```

> **Tip:** The development settings already include `.ngrok-free.app` in `ALLOWED_HOSTS`, so you only need `CSRF_TRUSTED_ORIGINS` and `ACCOUNT_DEFAULT_HTTP_PROTOCOL` if not already set.

Restart the Django server after changing `.env`.

### 5. Log in via CommCare

1. Open `https://abc123.ngrok-free.app` in your browser (use the ngrok URL, not localhost).
2. Click **Sign in with CommCare**.
3. Authorise the application in CommCare HQ.
4. You are redirected back and logged in. Scout automatically fetches your CommCare domains and creates `TenantMembership` records for each one.

### 6. Load cases

Follow the same steps as the API key path — ask the agent to "load my cases" in the chat interface.

---

## Managed database setup

The managed database is where Scout stores materialized CommCare case data. Each CommCare domain gets its own PostgreSQL schema.

**In development**, when `MANAGED_DATABASE_URL` is not set, Scout automatically falls back to your main application database (`DATABASE_URL`) with per-domain schema isolation. This works for local testing with no extra configuration.

To use a dedicated database (closer to the production setup):

```bash
# Create a second database
createdb scout_managed

# Set in .env
MANAGED_DATABASE_URL=postgresql://user:password@localhost/scout_managed
```

Scout creates schemas on demand — no migrations are needed for the managed database.

---

## Troubleshooting

**"No tenant selected" errors in the chat**

The agent requires an active tenant (CommCare domain) to be selected. If you have multiple domains, click the domain selector in the sidebar to choose one.

**Materialization fails with a connection error**

Check that `MANAGED_DATABASE_URL` points to a reachable PostgreSQL instance and that the user has `CREATE SCHEMA` privileges.

**OAuth callback returns a 400 CSRF error**

Ensure `CSRF_TRUSTED_ORIGINS` includes your ngrok URL with the `https://` scheme, and that you accessed the app via the ngrok URL (not localhost) throughout the OAuth flow.

**CommCare returns 401 during domain resolution**

Your OAuth token may have expired (they typically last 1 hour). Log out and log back in via CommCare to get a fresh token. API key credentials do not expire.
