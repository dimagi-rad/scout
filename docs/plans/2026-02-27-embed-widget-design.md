# Scout Embed Widget SDK Design

**Date:** 2026-02-27
**Status:** Approved

## Goal

Enable embedding Scout into external applications (starting with CommCare Connect Labs) via a JavaScript widget SDK that wraps a configurable iframe.

## Architecture

### Scout Side

#### 1. Frontend Embed Route (`/embed/`)

New React route that renders a configurable subset of the Scout app without shell chrome (sidebar, header). Controlled by URL query params:

| Param | Values | Default | Description |
|-------|--------|---------|-------------|
| `mode` | `chat`, `chat+artifacts`, `full` | `chat` | Which features to show |
| `tenant` | string | none | Pre-selects project by tenant ID |
| `theme` | `light`, `dark`, `auto` | `auto` | Color scheme |

**Mode behavior:**
- `chat` — Chat panel only, full width
- `chat+artifacts` — Chat panel + artifact panel when artifacts are generated
- `full` — Minimal sidebar nav (chat, artifacts, knowledge, recipes) + main content, project switcher visible

#### 2. Widget SDK (`widget.js`)

~2-3KB vanilla JS file served at `/widget.js`. No dependencies, no build step required by host apps.

**Host app usage:**

```html
<script>
  window.ScoutWidget = window.ScoutWidget || { _q: [] };
  ScoutWidget.init = function(opts) { ScoutWidget._q.push(['init', opts]); };
</script>
<script async src="https://scout.example.com/widget.js"></script>
<script>
  ScoutWidget.init({
    container: "#scout-container",
    tenant: "abc123",
    mode: "chat",
    theme: "auto",
    onReady: () => {},
    onEvent: (event) => {},
  });
</script>
```

**SDK responsibilities:**
- Creates iframe pointing to `/embed/?mode=X&tenant=Y&theme=Z`
- Manages iframe sizing (100% of container)
- Replays queued calls from async loading stub
- Shows loading spinner while iframe loads, error message if unreachable
- Listens for `postMessage` events from iframe, forwards to `onEvent`
- Validates `event.origin` against Scout origin (never uses `"*"`)
- Provides methods: `destroy()`, `setTenant(id)`, `setMode(mode)`

**postMessage events (iframe → host):**
- `scout:ready` — embed loaded and authenticated
- `scout:auth-required` — user needs to log in
- `scout:artifact-created` — artifact generated (includes ID)
- `scout:thread-created` — new chat thread started
- `scout:error` — error occurred

**postMessage commands (host → iframe):**
- `scout:set-tenant` — switch project
- `scout:set-mode` — change display mode
- `scout:navigate` — go to a specific view

#### 3. Security Middleware

**EmbedFrameOptionsMiddleware:**
- Applies only to `/embed/` routes
- Reads `EMBED_ALLOWED_ORIGINS` from Django settings
- Sets `Content-Security-Policy: frame-ancestors <origins>`
- Removes `X-Frame-Options` header for embed routes
- Non-embed routes keep `X-Frame-Options: DENY`

**Embed cookie handling:**
- For `/embed/` requests, patches session and CSRF cookies with `SameSite=None; Secure`
- Normal Scout usage keeps `SameSite=Lax`

**CSRF:**
- Existing flow works inside iframe (fetches `/api/auth/csrf/` as usual)
- `CSRF_TRUSTED_ORIGINS` must include host app origins

**Origin validation:**
- postMessage listeners validate `event.origin` on both sides

#### 4. Django Settings

```python
EMBED_ALLOWED_ORIGINS = [
    "https://connect-labs.example.com",
    "http://localhost:8001",  # dev
]
# Also add to CSRF_TRUSTED_ORIGINS
```

### Connect Labs Side

#### 1. Django View

New view at `/labs/scout/` that renders a template with the widget snippet. Passes `tenant_id` from session context (opportunity ID).

#### 2. Template

```html
{% extends "labs/base.html" %}
{% block content %}
<div id="scout-container" style="height: calc(100vh - 64px);"></div>
<script>
  window.ScoutWidget = window.ScoutWidget || { _q: [] };
  ScoutWidget.init = function(opts) { ScoutWidget._q.push(['init', opts]); };
</script>
<script async src="{{ scout_base_url }}/widget.js"></script>
<script>
  ScoutWidget.init({
    container: "#scout-container",
    tenant: "{{ tenant_id }}",
    mode: "chat+artifacts",
    theme: "auto",
  });
</script>
{% endblock %}
```

#### 3. Configuration

- `SCOUT_BASE_URL` setting
- "Scout" link in sidebar navigation

### Authentication (Phase 1)

User authenticates via existing CommCare OAuth flow inside the iframe. This means:
- First visit requires OAuth login within the iframe
- Session persists for subsequent visits
- Cross-origin cookies require `SameSite=None; Secure`

### Authentication (Future)

Server-signed embed tokens: Connect Labs backend requests a short-lived token from Scout, passes it in the iframe URL. User skips OAuth entirely.

## Out of Scope

- Server-signed embed tokens
- Floating chat widget mode
- Versioned SDK URL (`/widget/v1.js`)
- CSP nonce support
- Per-tenant origin restrictions
- Rate limiting on embed endpoints
