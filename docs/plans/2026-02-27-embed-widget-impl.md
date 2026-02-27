# Scout Embed Widget SDK Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable embedding Scout into external applications via a JavaScript widget SDK that wraps a configurable iframe.

**Architecture:** A new `/embed/` frontend route renders Scout components without shell chrome, controlled by URL params (mode, tenant, theme). A vanilla JS SDK (`widget.js`) served by Django creates the iframe, manages postMessage communication, and provides a clean API for host apps. Django middleware handles frame-ancestors CSP and cross-origin cookie requirements for embed routes.

**Tech Stack:** React 19, TypeScript, vanilla JS (widget SDK), Django 5 middleware, Vite

**Design doc:** `docs/plans/2026-02-27-embed-widget-design.md`

---

### Task 1: Django Embed Middleware — Frame Ancestors

**Files:**
- Create: `config/middleware/embed.py`
- Modify: `config/settings/base.py:68-77` (MIDDLEWARE list)
- Modify: `config/settings/base.py` (add EMBED_ALLOWED_ORIGINS setting)
- Test: `tests/test_embed_middleware.py`

**Step 1: Write the failing test**

```python
# tests/test_embed_middleware.py
import pytest
from django.test import RequestFactory, override_settings

from config.middleware.embed import EmbedFrameOptionsMiddleware


@pytest.mark.django_db
class TestEmbedFrameOptionsMiddleware:
    def setup_method(self):
        self.factory = RequestFactory()
        self.get_response = lambda request: self._response
        self.middleware = EmbedFrameOptionsMiddleware(self.get_response)

    def _make_response(self, status=200):
        from django.http import HttpResponse
        self._response = HttpResponse("OK")
        self._response["X-Frame-Options"] = "DENY"
        return self._response

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_embed_route_removes_x_frame_options(self):
        self._make_response()
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        assert "X-Frame-Options" not in response

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_embed_route_sets_frame_ancestors(self):
        self._make_response()
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        assert "frame-ancestors" in response.get("Content-Security-Policy", "")
        assert "https://connect-labs.example.com" in response["Content-Security-Policy"]

    @override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
    def test_non_embed_route_keeps_x_frame_options(self):
        self._make_response()
        request = self.factory.get("/api/chat/")
        response = self.middleware(request)
        assert response.get("X-Frame-Options") == "DENY"

    @override_settings(EMBED_ALLOWED_ORIGINS=[])
    def test_empty_origins_denies_framing(self):
        self._make_response()
        request = self.factory.get("/embed/")
        response = self.middleware(request)
        assert response.get("X-Frame-Options") == "DENY"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_embed_middleware.py -v`
Expected: FAIL with ImportError (module doesn't exist yet)

**Step 3: Write the middleware**

```python
# config/middleware/__init__.py
```

```python
# config/middleware/embed.py
from django.conf import settings
from django.http import HttpRequest, HttpResponse


class EmbedFrameOptionsMiddleware:
    """Allow iframe embedding for /embed/ routes from configured origins."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not request.path.startswith("/embed/"):
            return response

        allowed_origins = getattr(settings, "EMBED_ALLOWED_ORIGINS", [])
        if not allowed_origins:
            return response

        # Remove X-Frame-Options (conflicts with CSP frame-ancestors)
        if "X-Frame-Options" in response:
            del response["X-Frame-Options"]

        # Set CSP frame-ancestors
        origins = " ".join(allowed_origins)
        response["Content-Security-Policy"] = f"frame-ancestors 'self' {origins}"
        return response
```

**Step 4: Add settings and middleware registration**

Add to `config/settings/base.py` after line 270 (SESSION_COOKIE_NAME):

```python
# Embed widget settings
EMBED_ALLOWED_ORIGINS = env.list("EMBED_ALLOWED_ORIGINS", default=[])
```

Add the middleware to the MIDDLEWARE list in `config/settings/base.py` at line 76 (after XFrameOptionsMiddleware):

```python
    "config.middleware.embed.EmbedFrameOptionsMiddleware",
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_embed_middleware.py -v`
Expected: All 4 tests PASS

**Step 6: Commit**

```bash
git add config/middleware/ tests/test_embed_middleware.py config/settings/base.py
git commit -m "feat: add embed frame-ancestors middleware for iframe support"
```

---

### Task 2: Django Embed Middleware — Cross-Origin Cookies

**Files:**
- Modify: `config/middleware/embed.py`
- Test: `tests/test_embed_middleware.py`

**Step 1: Write the failing test**

Add to `tests/test_embed_middleware.py`:

```python
@override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
def test_embed_route_sets_samesite_none_on_cookies(self):
    self._make_response()
    self._response.set_cookie("sessionid_scout", "abc123")
    self._response.set_cookie("csrftoken_scout", "xyz789")
    request = self.factory.get("/embed/")
    response = self.middleware(request)
    for cookie_name in ["sessionid_scout", "csrftoken_scout"]:
        cookie = response.cookies[cookie_name]
        assert cookie["samesite"] == "None"
        assert cookie["secure"] is True

@override_settings(EMBED_ALLOWED_ORIGINS=["https://connect-labs.example.com"])
def test_non_embed_route_does_not_change_cookies(self):
    self._make_response()
    self._response.set_cookie("sessionid_scout", "abc123", samesite="Lax")
    request = self.factory.get("/api/chat/")
    response = self.middleware(request)
    assert response.cookies["sessionid_scout"]["samesite"] == "Lax"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_embed_middleware.py::TestEmbedFrameOptionsMiddleware::test_embed_route_sets_samesite_none_on_cookies -v`
Expected: FAIL

**Step 3: Add cookie patching to middleware**

Add to the `__call__` method in `config/middleware/embed.py`, after the CSP header is set:

```python
        # Patch cookies for cross-origin iframe usage
        for cookie_name in response.cookies:
            response.cookies[cookie_name]["samesite"] = "None"
            response.cookies[cookie_name]["secure"] = True
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_embed_middleware.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add config/middleware/embed.py tests/test_embed_middleware.py
git commit -m "feat: patch cookies with SameSite=None for embed routes"
```

---

### Task 3: Django Widget.js Endpoint

**Files:**
- Create: `config/views.py`
- Modify: `config/urls.py`
- Test: `tests/test_embed_middleware.py` (add widget view tests)

**Step 1: Write the failing test**

Add to `tests/test_embed_middleware.py`:

```python
from django.test import Client


class TestWidgetJSView:
    def test_widget_js_returns_javascript(self):
        client = Client()
        response = client.get("/widget.js")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/javascript"

    def test_widget_js_contains_scout_widget(self):
        client = Client()
        response = client.get("/widget.js")
        content = response.content.decode()
        assert "ScoutWidget" in content

    def test_widget_js_has_cors_header(self):
        client = Client()
        response = client.get("/widget.js")
        assert response.get("Access-Control-Allow-Origin") == "*"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_embed_middleware.py::TestWidgetJSView -v`
Expected: FAIL with 404

**Step 3: Create the view and URL**

```python
# config/views.py
from django.http import HttpResponse
from django.views.decorators.cache import cache_control


@cache_control(public=True, max_age=3600)
def widget_js_view(request):
    """Serve the Scout embed widget SDK."""
    from pathlib import Path

    widget_path = Path(__file__).parent.parent / "frontend" / "public" / "widget.js"
    try:
        content = widget_path.read_text()
    except FileNotFoundError:
        content = "// widget.js not found"

    response = HttpResponse(content, content_type="application/javascript")
    response["Access-Control-Allow-Origin"] = "*"
    return response
```

Add to `config/urls.py` (before the urlpatterns list, add import; then add URL):

Import: `from config.views import widget_js_view`

URL pattern (add before the health check):
```python
    path("widget.js", widget_js_view, name="widget-js"),
```

**Step 4: Create a placeholder widget.js**

Create `frontend/public/widget.js` with a placeholder:

```javascript
// Scout Widget SDK - placeholder
(function() { window.ScoutWidget = { init: function() {} }; })();
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_embed_middleware.py::TestWidgetJSView -v`
Expected: All 3 tests PASS

**Step 6: Commit**

```bash
git add config/views.py config/urls.py frontend/public/widget.js tests/test_embed_middleware.py
git commit -m "feat: add /widget.js endpoint for embed SDK"
```

---

### Task 4: Widget SDK (vanilla JS)

**Files:**
- Create: `frontend/public/widget.js`

This is the main SDK file. No test framework needed — it's vanilla JS tested via the Django endpoint tests and manual browser testing.

**Step 1: Write the widget SDK**

```javascript
// frontend/public/widget.js
(function () {
  "use strict";

  var SCOUT_ORIGIN = (function () {
    var scripts = document.getElementsByTagName("script");
    for (var i = 0; i < scripts.length; i++) {
      var src = scripts[i].src || "";
      if (src.indexOf("widget.js") !== -1) {
        var url = new URL(src);
        return url.origin;
      }
    }
    return window.location.origin;
  })();

  var instances = {};
  var instanceId = 0;

  function ScoutWidgetInstance(opts) {
    this.id = ++instanceId;
    this.opts = opts;
    this.iframe = null;
    this.container = null;
    this.ready = false;
    this._boundMessageHandler = this._onMessage.bind(this);
    this._init();
  }

  ScoutWidgetInstance.prototype._init = function () {
    // Resolve container
    if (typeof this.opts.container === "string") {
      this.container = document.querySelector(this.opts.container);
    } else {
      this.container = this.opts.container;
    }
    if (!this.container) {
      console.error("[ScoutWidget] Container not found:", this.opts.container);
      return;
    }

    // Show loading state
    this.container.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;' +
      'height:100%;width:100%;font-family:system-ui,sans-serif;color:#666;">' +
      '<div style="text-align:center;">' +
      '<div style="width:24px;height:24px;border:3px solid #e5e7eb;' +
      'border-top-color:#6366f1;border-radius:50%;animation:scout-spin 0.8s linear infinite;' +
      'margin:0 auto 8px;"></div>Loading Scout...</div></div>';

    // Add spinner animation
    if (!document.getElementById("scout-widget-styles")) {
      var style = document.createElement("style");
      style.id = "scout-widget-styles";
      style.textContent =
        "@keyframes scout-spin{to{transform:rotate(360deg)}}";
      document.head.appendChild(style);
    }

    // Build iframe URL
    var params = [];
    if (this.opts.mode) params.push("mode=" + encodeURIComponent(this.opts.mode));
    if (this.opts.tenant) params.push("tenant=" + encodeURIComponent(this.opts.tenant));
    if (this.opts.theme) params.push("theme=" + encodeURIComponent(this.opts.theme));
    var src = SCOUT_ORIGIN + "/embed/" + (params.length ? "?" + params.join("&") : "");

    // Create iframe
    this.iframe = document.createElement("iframe");
    this.iframe.src = src;
    this.iframe.style.cssText =
      "width:100%;height:100%;border:none;display:block;";
    this.iframe.setAttribute("allow", "clipboard-write");
    this.iframe.setAttribute("title", "Scout");

    // Listen for messages
    window.addEventListener("message", this._boundMessageHandler);

    // Replace loading state with iframe
    this.iframe.onload = function () {
      // iframe loaded, but we wait for scout:ready postMessage
    };

    this.iframe.onerror = function () {
      this.container.innerHTML =
        '<div style="display:flex;align-items:center;justify-content:center;' +
        'height:100%;font-family:system-ui,sans-serif;color:#ef4444;">' +
        "Failed to load Scout</div>";
    }.bind(this);

    this.container.innerHTML = "";
    this.container.appendChild(this.iframe);

    instances[this.id] = this;
  };

  ScoutWidgetInstance.prototype._onMessage = function (event) {
    if (event.origin !== SCOUT_ORIGIN) return;
    var data = event.data;
    if (!data || typeof data.type !== "string" || !data.type.startsWith("scout:")) return;

    if (data.type === "scout:ready") {
      this.ready = true;
      if (typeof this.opts.onReady === "function") this.opts.onReady();
    }

    if (typeof this.opts.onEvent === "function") {
      this.opts.onEvent(data);
    }
  };

  ScoutWidgetInstance.prototype._postMessage = function (type, payload) {
    if (!this.iframe || !this.iframe.contentWindow) return;
    this.iframe.contentWindow.postMessage(
      { type: type, payload: payload },
      SCOUT_ORIGIN
    );
  };

  ScoutWidgetInstance.prototype.setTenant = function (tenantId) {
    this._postMessage("scout:set-tenant", { tenant: tenantId });
  };

  ScoutWidgetInstance.prototype.setMode = function (mode) {
    this._postMessage("scout:set-mode", { mode: mode });
  };

  ScoutWidgetInstance.prototype.destroy = function () {
    window.removeEventListener("message", this._boundMessageHandler);
    if (this.iframe && this.iframe.parentNode) {
      this.iframe.parentNode.removeChild(this.iframe);
    }
    delete instances[this.id];
  };

  // Public API
  var ScoutWidget = {
    init: function (opts) {
      return new ScoutWidgetInstance(opts || {});
    },
    destroy: function () {
      Object.keys(instances).forEach(function (id) {
        instances[id].destroy();
      });
    },
  };

  // Replay queued calls from async loading stub
  var queued = window.ScoutWidget && window.ScoutWidget._q;
  window.ScoutWidget = ScoutWidget;
  if (queued && queued.length) {
    queued.forEach(function (call) {
      var method = call[0];
      var args = call[1];
      if (typeof ScoutWidget[method] === "function") {
        ScoutWidget[method](args);
      }
    });
  }
})();
```

**Step 2: Run Django widget tests to verify it serves correctly**

Run: `uv run pytest tests/test_embed_middleware.py::TestWidgetJSView -v`
Expected: All 3 tests PASS

**Step 3: Commit**

```bash
git add frontend/public/widget.js
git commit -m "feat: implement Scout widget SDK with iframe management and postMessage bridge"
```

---

### Task 5: Frontend Embed Route & EmbedLayout

**Files:**
- Create: `frontend/src/components/EmbedLayout/EmbedLayout.tsx`
- Create: `frontend/src/pages/EmbedPage.tsx`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/App.tsx`

**Step 1: Create the EmbedLayout component**

```typescript
// frontend/src/components/EmbedLayout/EmbedLayout.tsx
import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { useEmbedParams } from "@/hooks/useEmbedParams"

export function EmbedLayout() {
  const { mode } = useEmbedParams()
  const showSidebar = mode === "full"
  const showArtifacts = mode === "full" || mode === "chat+artifacts"

  return (
    <div className="flex h-screen">
      {showSidebar && <Sidebar />}
      <main className="flex-1 min-w-0 overflow-auto">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
      {showArtifacts && <ArtifactPanel />}
    </div>
  )
}
```

**Step 2: Create the useEmbedParams hook**

```typescript
// frontend/src/hooks/useEmbedParams.ts
import { useMemo } from "react"

export type EmbedMode = "chat" | "chat+artifacts" | "full"
export type EmbedTheme = "light" | "dark" | "auto"

export interface EmbedParams {
  mode: EmbedMode
  tenant: string | null
  theme: EmbedTheme
  isEmbed: boolean
}

export function useEmbedParams(): EmbedParams {
  return useMemo(() => {
    const params = new URLSearchParams(window.location.search)
    const isEmbed = window.location.pathname.startsWith("/embed")
    return {
      mode: (params.get("mode") as EmbedMode) || "chat",
      tenant: params.get("tenant"),
      theme: (params.get("theme") as EmbedTheme) || "auto",
      isEmbed,
    }
  }, [])
}
```

**Step 3: Create the EmbedPage entry point**

```typescript
// frontend/src/pages/EmbedPage.tsx
import { useEffect } from "react"
import { RouterProvider, createBrowserRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { Skeleton } from "@/components/ui/skeleton"
import { EmbedLayout } from "@/components/EmbedLayout/EmbedLayout"
import { ChatPanel } from "@/components/ChatPanel/ChatPanel"
import { ArtifactsPage } from "@/pages/ArtifactsPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { RecipesPage } from "@/pages/RecipesPage"
import { useEmbedParams } from "@/hooks/useEmbedParams"

function notifyParent(type: string, payload?: Record<string, unknown>) {
  if (window.parent !== window) {
    window.parent.postMessage({ type, ...payload }, "*")
  }
}

const embedRouter = createBrowserRouter([
  {
    path: "/embed",
    element: <EmbedLayout />,
    children: [
      { index: true, element: <ChatPanel /> },
      { path: "chat", element: <ChatPanel /> },
      { path: "artifacts", element: <ArtifactsPage /> },
      { path: "knowledge", element: <KnowledgePage /> },
      { path: "knowledge/new", element: <KnowledgePage /> },
      { path: "knowledge/:id", element: <KnowledgePage /> },
      { path: "recipes", element: <RecipesPage /> },
      { path: "recipes/:id", element: <RecipesPage /> },
    ],
  },
])

export function EmbedPage() {
  const authStatus = useAppStore((s) => s.authStatus)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const { tenant } = useEmbedParams()

  useEffect(() => {
    fetchMe()
  }, [fetchMe])

  useEffect(() => {
    if (authStatus === "authenticated") {
      notifyParent("scout:ready")
    } else if (authStatus === "unauthenticated") {
      notifyParent("scout:auth-required")
    }
  }, [authStatus])

  if (authStatus === "idle" || authStatus === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="space-y-3 w-64">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      </div>
    )
  }

  if (authStatus === "unauthenticated") {
    return <LoginForm />
  }

  return <RouterProvider router={embedRouter} />
}
```

**Step 4: Update App.tsx to detect embed routes**

Modify `frontend/src/App.tsx` to add embed detection alongside existing public page detection:

```typescript
// Add import at top
import { EmbedPage } from "@/pages/EmbedPage"

// Update the isPublicPage line (line 22) to also detect embed:
const isPublicPage = /^\/shared\/(runs|threads)\/[^/]+\/?$/.test(window.location.pathname)
const isEmbedPage = window.location.pathname.startsWith("/embed")

// Update the useEffect to skip auth fetch for public pages (embed handles its own):
useEffect(() => {
  if (!isPublicPage && !isEmbedPage) {
    fetchMe()
  }
}, [fetchMe, isPublicPage, isEmbedPage])

// Add embed check after public page check (after line 32):
if (isEmbedPage) {
  return <EmbedPage />
}
```

**Step 5: Run frontend lint to verify no errors**

Run: `cd frontend && bun run lint`
Expected: No errors

**Step 6: Commit**

```bash
git add frontend/src/components/EmbedLayout/ frontend/src/pages/EmbedPage.tsx frontend/src/hooks/useEmbedParams.ts frontend/src/App.tsx frontend/src/router.tsx
git commit -m "feat: add /embed/ route with configurable layout modes"
```

---

### Task 6: PostMessage Bridge — Iframe Side

**Files:**
- Create: `frontend/src/hooks/useEmbedMessaging.ts`
- Modify: `frontend/src/pages/EmbedPage.tsx`

**Step 1: Create the messaging hook**

```typescript
// frontend/src/hooks/useEmbedMessaging.ts
import { useEffect, useCallback } from "react"
import { useEmbedParams } from "./useEmbedParams"

type MessageHandler = (type: string, payload: Record<string, unknown>) => void

export function useEmbedMessaging(onCommand?: MessageHandler) {
  const { isEmbed } = useEmbedParams()

  const sendEvent = useCallback(
    (type: string, payload?: Record<string, unknown>) => {
      if (!isEmbed || window.parent === window) return
      window.parent.postMessage({ type, ...payload }, "*")
    },
    [isEmbed]
  )

  useEffect(() => {
    if (!isEmbed || !onCommand) return

    function handleMessage(event: MessageEvent) {
      const data = event.data
      if (!data || typeof data.type !== "string" || !data.type.startsWith("scout:")) return
      onCommand(data.type, data.payload || {})
    }

    window.addEventListener("message", handleMessage)
    return () => window.removeEventListener("message", handleMessage)
  }, [isEmbed, onCommand])

  return { sendEvent }
}
```

**Step 2: Wire messaging into EmbedPage**

Update `EmbedPage.tsx` to use `useEmbedMessaging` instead of the inline `notifyParent` function, and handle incoming commands:

Replace the `notifyParent` function and auth effect with:

```typescript
const handleCommand = useCallback((type: string, payload: Record<string, unknown>) => {
  if (type === "scout:set-tenant") {
    // Future: switch tenant/project context
    console.log("[Scout Embed] set-tenant:", payload.tenant)
  }
  if (type === "scout:set-mode") {
    // Future: update mode dynamically
    console.log("[Scout Embed] set-mode:", payload.mode)
  }
}, [])

const { sendEvent } = useEmbedMessaging(handleCommand)

useEffect(() => {
  if (authStatus === "authenticated") {
    sendEvent("scout:ready")
  } else if (authStatus === "unauthenticated") {
    sendEvent("scout:auth-required")
  }
}, [authStatus, sendEvent])
```

**Step 3: Run frontend lint**

Run: `cd frontend && bun run lint`
Expected: No errors

**Step 4: Commit**

```bash
git add frontend/src/hooks/useEmbedMessaging.ts frontend/src/pages/EmbedPage.tsx
git commit -m "feat: add postMessage bridge for embed iframe communication"
```

---

### Task 7: Update CSRF Trusted Origins for Embed

**Files:**
- Modify: `config/settings/base.py`
- Modify: `config/settings/development.py`

**Step 1: Update development settings**

Add to `config/settings/development.py` to extend CSRF_TRUSTED_ORIGINS for local embed development:

```python
# Allow local Connect Labs to embed Scout
EMBED_ALLOWED_ORIGINS = ["http://localhost:8001", "http://localhost:3000"]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:5173",  # Vite dev
    "http://localhost:8001",  # Connect Labs dev
    "http://localhost:3000",  # Connect Labs frontend dev
]
```

**Step 2: Run existing tests to make sure nothing breaks**

Run: `uv run pytest tests/ -v --timeout=30`
Expected: All existing tests PASS

**Step 3: Commit**

```bash
git add config/settings/development.py config/settings/base.py
git commit -m "feat: configure embed origins for development"
```

---

### Task 8: Frontend Build Verification & Manual Testing

**Step 1: Run the frontend TypeScript check**

Run: `cd frontend && bun run build`
Expected: Build succeeds without type errors

**Step 2: Run the frontend lint**

Run: `cd frontend && bun run lint`
Expected: No lint errors

**Step 3: Run all backend tests**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS

**Step 4: Commit any fixes if needed, then tag the feature**

```bash
git add -A
git commit -m "feat: Scout embed widget SDK complete

- Django middleware for frame-ancestors CSP and cross-origin cookies
- /widget.js endpoint serving vanilla JS SDK
- Widget SDK with iframe management, postMessage bridge, async loading
- /embed/ frontend route with configurable mode (chat, chat+artifacts, full)
- EmbedLayout component without shell chrome
- PostMessage communication between host app and embed"
```

---

## Connect Labs Integration (separate repo)

These tasks are for the `commcare-connect` project at `/home/jjackson/projects/commcare-connect`. They should be done after the Scout tasks above are complete and merged.

### Task 9: Connect Labs — Scout Page View

**Files:**
- Create: `commcare_connect/labs/views/scout.py`
- Modify: `commcare_connect/labs/urls.py` (or equivalent URL config)
- Create: `templates/labs/scout.html`

**Step 1: Create the Django view**

```python
# commcare_connect/labs/views/scout.py
from django.conf import settings
from django.shortcuts import render


def scout_embed_view(request):
    """Render the Scout embed page."""
    context = {
        "scout_base_url": getattr(settings, "SCOUT_BASE_URL", "http://localhost:8000"),
        "tenant_id": getattr(request, "opportunity_id", "") or "",
        "scout_mode": request.GET.get("mode", "chat+artifacts"),
    }
    return render(request, "labs/scout.html", context)
```

**Step 2: Create the template**

```html
<!-- templates/labs/scout.html -->
{% extends "labs/base.html" %}

{% block content %}
<div id="scout-container" style="height: calc(100vh - 64px); width: 100%;"></div>

<script>
  window.ScoutWidget = window.ScoutWidget || { _q: [] };
  ScoutWidget.init = function(opts) { ScoutWidget._q.push(['init', opts]); };
</script>
<script async src="{{ scout_base_url }}/widget.js"></script>
<script>
  ScoutWidget.init({
    container: "#scout-container",
    tenant: "{{ tenant_id }}",
    mode: "{{ scout_mode }}",
    theme: "auto",
  });
</script>
{% endblock %}
```

**Step 3: Add the URL pattern**

Add to the labs URL config:

```python
from commcare_connect.labs.views.scout import scout_embed_view

path("labs/scout/", scout_embed_view, name="labs-scout"),
```

**Step 4: Add settings**

```python
# In labs settings
SCOUT_BASE_URL = env("SCOUT_BASE_URL", default="http://localhost:8000")
```

**Step 5: Add nav link**

Add a "Scout" link in the labs sidebar/navigation that points to `/labs/scout/`.

**Step 6: Commit**

```bash
git add commcare_connect/labs/views/scout.py templates/labs/scout.html
git commit -m "feat: add Scout embed page to Connect Labs"
```
