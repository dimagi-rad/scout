# OAuth UI Design: Login Buttons & Connected Accounts

## Problem

The backend has full OAuth support (Google, GitHub, CommCare, CommCare Connect) via django-allauth, but the frontend only shows email/password login. Users have no way to:
1. Log in via OAuth providers
2. Connect additional providers after login (e.g., log in with Google, then connect CommCare for data access)

## Design

### Backend: Provider discovery API

New endpoint in `apps/chat/views.py` (or a new `apps/users/views.py`):

**`GET /api/auth/providers/`** (no auth required)

Queries allauth's `SocialApp` model to return providers configured for the current site. If the request is authenticated, includes connection status.

```json
{
  "providers": [
    {
      "id": "google",
      "name": "Google",
      "login_url": "/accounts/google/login/",
      "connected": true,
      "is_login_provider": true
    },
    {
      "id": "commcare",
      "name": "CommCare",
      "login_url": "/accounts/commcare/login/",
      "connected": false,
      "is_login_provider": false
    }
  ]
}
```

The `is_login_provider` flag distinguishes identity providers (Google, GitHub) from data-access providers (CommCare, CommCare Connect). Both can be used for login, but the UI may present them differently.

**`POST /api/auth/providers/<provider_id>/disconnect/`** (auth required)

Disconnects a social account. Returns 400 if it's the user's only login method (no password set and no other connected provider).

```json
// Success
{"status": "disconnected"}

// Error
{"error": "Cannot disconnect your only login method"}
```

### Frontend: Login page

`LoginForm.tsx` calls `GET /api/auth/providers/` on mount. Below the email/password form:

1. Visual divider with "or continue with" text
2. One button per configured provider with provider name
3. Clicking navigates to `login_url + "?next=/"` (full-page redirect to allauth, which handles OAuth and redirects back)

### Frontend: Connected accounts page

New route: `/settings/connections`

New component: `ConnectionsPage`

Displays each configured provider with:
- Provider name
- Connection status (connected / not connected)
- Connect button: navigates to `login_url + "?process=connect&next=/settings/connections"` (allauth's `process=connect` links to existing user)
- Disconnect button: calls `POST /api/auth/providers/<id>/disconnect/`, refreshes list
- Guard: disable disconnect if it's the only login method

Accessed via a "Connected Accounts" link in the sidebar user section (bottom).

### Data flow

```
Login:
  LoginForm → GET /api/auth/providers/ → render buttons
  Click → /accounts/google/login/?next=/ → OAuth dance → session cookie → SPA loads

Connect (post-login):
  ConnectionsPage → GET /api/auth/providers/ → render status
  Click Connect → /accounts/commcare/login/?process=connect&next=/settings/connections → OAuth → redirect back

Disconnect:
  ConnectionsPage → POST /api/auth/providers/commcare/disconnect/ → refresh provider list
```

### Files to create/modify

**Backend:**
- `apps/chat/views.py` — add `providers_view` and `disconnect_provider_view`
- `config/urls.py` — add routes for new endpoints

**Frontend:**
- `frontend/src/components/LoginForm/LoginForm.tsx` — add provider buttons
- `frontend/src/pages/ConnectionsPage/ConnectionsPage.tsx` — new page
- `frontend/src/router.tsx` — add `/settings/connections` route
- `frontend/src/components/Sidebar/Sidebar.tsx` — add Connected Accounts link
- `frontend/src/api/client.ts` — no changes needed (existing fetch wrapper works)

### Edge cases

- **No providers configured**: Login page shows only email/password. Connected accounts page shows empty state.
- **Disconnect last method**: API returns 400, frontend shows error.
- **OAuth error**: allauth handles errors and redirects to error page. Frontend should handle `?error=` query param on redirect back.
- **Already connected**: allauth's `process=connect` is idempotent for the same provider.
