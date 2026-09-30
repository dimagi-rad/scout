# Testing CommCare integration in development

Scout integrates with CommCare HQ to materialize case and form data for AI-powered querying. This guide covers two ways to connect a CommCare domain during local development:

- **API key** — the simpler path; no tunneling required
- **OAuth via ngrok** — required if you want to test the full OAuth flow

## Prerequisites

Complete [installation](installation.md) first, using the Honcho setup so the API, MCP server, background worker and frontend are all running. You also need a CommCare HQ account with access to at least one domain.

---

## Option A: API key (recommended for dev)

This path connects a domain with a CommCare username and API key from the onboarding wizard — no OAuth app setup or external tunnel needed.

### 1. Create an API key in CommCare HQ

1. Log in to [CommCare HQ](https://www.commcarehq.org).
2. Go to **Settings → My Account → API Keys**.
3. Create a new key and copy the value. The key is shown once.

### 2. Log in to Scout

1. Open `http://localhost:5173`.
2. Log in with the superuser account you created during installation.
3. Because the account has no data connection yet, the **Connect your CommCare data** wizard appears.

### 3. Connect your CommCare domain

1. Click **Use an API Key**.
2. If your project space is on EU CommCare HQ (`eu.commcarehq.org`), pick **EU** as the **CommCare HQ server**. Otherwise leave it on Global (`www.commcarehq.org`).
3. Enter your CommCare **domain** (the slug from the URL, e.g. `my-project` from `www.commcarehq.org/a/my-project/`).
4. Enter your CommCare **username** (your account email) and the **API key**.
5. Click **Connect**.

Scout verifies the key against CommCare, stores it encrypted with `DB_CREDENTIAL_KEY`, and records a connection and a membership for the domain. A workspace for the domain is created automatically, with you as its manager.

### 4. Load data and start querying

Open the workspace's chat and ask the agent to load the data, for example:

```
Load my data
```

or use the `/refresh-data` slash command. The agent calls the `run_materialization` MCP tool, which queues a background job. The worker fetches the domain's cases and forms from CommCare and writes them to the domain's own PostgreSQL schema in the managed database. When the job finishes, the conversation resumes automatically.

> **Note:** Materialization needs a running worker and `MANAGED_DATABASE_URL` (see [Managed database setup](#managed-database-setup) below). Under the development settings the managed database defaults to your main app database, so no extra setup is needed.

---

## Option B: OAuth flow via ngrok

Use this path to test the full CommCare OAuth flow. CommCare redirects back to Scout after authorisation, so Scout needs a publicly reachable HTTPS URL.

### 1. Start ngrok in front of the Vite dev server

```bash
# Install ngrok (https://ngrok.com/download) then:
ngrok http 5173
```

The Vite dev server proxies `/api`, `/accounts` and `/health` to Django and already accepts `.ngrok-free.app` hosts. Note the HTTPS forwarding URL — it looks like `https://abc123.ngrok-free.app`.

### 2. Create a CommCare OAuth application

1. In CommCare HQ, create a new **Confidential** OAuth2 application (or ask your CommCare admin to create one).
2. Set the **Redirect URI** to:

   ```
   https://abc123.ngrok-free.app/accounts/commcare/login/callback/
   ```

3. Note the **Client ID** and **Client Secret**.

### 3. Register the OAuth app in Scout

Either set `COMMCARE_OAUTH_CLIENT_ID` and `COMMCARE_OAUTH_CLIENT_SECRET` in your environment and run:

```bash
uv run python manage.py setup_oauth_apps --domain abc123.ngrok-free.app
```

or add it by hand in Django admin (`http://localhost:8000/admin/`, **Social accounts → Social applications → Add**) with provider `CommCare`, your client ID and secret, and the site moved to **Chosen sites**.

### 4. Configure Django for the ngrok host

Add these lines to `.env` (replace the URL with your ngrok URL) and restart Django:

```bash
# Django rejects Host headers not in this list
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.ngrok-free.app

# So allauth builds https:// callback URLs
ACCOUNT_DEFAULT_HTTP_PROTOCOL=https
```

The development settings replace `CSRF_TRUSTED_ORIGINS` with a fixed list of localhost origins, so setting it in `.env` has no effect. For the duration of the test, add your ngrok origin (`https://abc123.ngrok-free.app`) to that list in `config/settings/development.py`, and don't commit the change.

By default, CommCare sign-in only accepts `dimagi.com` email addresses. To test with another account, override `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` (see `.env.example`).

### 5. Log in via CommCare

1. Open `https://abc123.ngrok-free.app` in your browser (use the ngrok URL, not localhost).
2. Under **or continue with**, click the button for the social application you registered (named **CommCare HQ** by `setup_oauth_apps`). If you're already logged in with a password account that has no connection yet, click **Connect with OAuth** in the onboarding wizard instead.
3. Authorise the application in CommCare HQ.
4. You are redirected back and logged in. Scout fetches your CommCare domains and creates a membership, and an auto-created workspace, for each one.

### 6. Load data

Follow the same steps as the API key path — ask the agent to load your data in the chat.

---

## Managed database setup

The managed database is where Scout stores materialized data. Each tenant (for example, a CommCare domain) gets its own PostgreSQL schema.

**In development**, when `MANAGED_DATABASE_URL` is not set, the development settings point it at your main application database (`DATABASE_URL`); tenant schemas still keep the data separate. This works for local testing with no extra configuration. Other settings modules have no fallback.

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

**Materialization never finishes**

Check that the Procrastinate worker is running (`worker` in Honcho's output). Without it, the job stays queued.

**Materialization fails with a connection error**

Check that `MANAGED_DATABASE_URL` points to a reachable PostgreSQL instance and that the user has `CREATE SCHEMA` privileges.

**OAuth sign-in fails with a 403 CSRF error**

Check that your ngrok origin, with the `https://` scheme, is in `CSRF_TRUSTED_ORIGINS` in the development settings, and that you used the ngrok URL (not localhost) throughout the OAuth flow.
