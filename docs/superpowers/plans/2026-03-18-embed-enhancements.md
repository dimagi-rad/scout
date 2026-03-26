# Embed Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-implement embed/iframe features from PRs #61 and #64 on the refactored upstream codebase — BASE_PATH support, OAuth popup flow for embeds, and iframe auto-resize.

**Architecture:** Three independent features layered on the existing embed infrastructure. BASE_PATH enables subpath deploys (`/scout/`). OAuth popup handles the X-Frame-Options restriction when authenticating inside an iframe. Auto-resize lets the iframe expand to fit its content via postMessage.

**Tech Stack:** React 19, Vite, TypeScript, Zustand, django-allauth, postMessage API, ResizeObserver

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `frontend/src/config.ts` | Create | Runtime `BASE_PATH` constant from `VITE_BASE_PATH` |
| `frontend/vite.config.ts` | Modify | Set `base` from `VITE_BASE_PATH` |
| `frontend/src/router.tsx` | Modify | Add `basename` to main router |
| `frontend/src/pages/EmbedPage.tsx` | Modify | Add `basename` to embed router, popup auth, visibility re-auth |
| `frontend/src/hooks/useEmbedParams.ts` | Modify | Strip `BASE_PATH` from pathname before detecting `/embed` |
| `frontend/src/App.tsx` | Modify | Strip `BASE_PATH` when detecting embed/public routes, handle `popup_close` |
| `frontend/src/components/LoginForm/LoginForm.tsx` | Modify | Use popup auth flow when in embed mode |
| `frontend/src/hooks/useAutoResize.ts` | Create | ResizeObserver hook that sends `scout:resize` to parent frame |
| `frontend/src/components/EmbedLayout/EmbedLayout.tsx` | Modify | Add `min-h-[600px]`, wire up `useAutoResize` |
| `frontend/public/widget.js` | Modify | Add version, `SCOUT_BASE` path detection, auto-resize handling, absolute iframe positioning |

---

### Task 1: BASE_PATH — Config and Vite

**Files:**
- Create: `frontend/src/config.ts`
- Modify: `frontend/vite.config.ts`

- [ ] **Step 1: Create `frontend/src/config.ts`**

```typescript
/**
 * Runtime base path for the app, derived from the Vite build-time env var.
 * Defaults to "" (root) for local development.
 *
 * Examples:
 *   local dev:    ""
 *   connect-labs: "/scout"
 */
export const BASE_PATH = (import.meta.env.VITE_BASE_PATH || "").replace(/\/$/, "")
```

- [ ] **Step 2: Add `base` to Vite config**

In `frontend/vite.config.ts`, add a `base` property using the loaded env:

```typescript
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "")

  return {
    base: env.VITE_BASE_PATH || "/",
    plugins: [react(), tailwindcss()],
    // ... rest unchanged
  }
})
```

This tells Vite to prefix all asset URLs with the base path during production builds.

- [ ] **Step 3: Verify build works**

Run: `cd frontend && bun run build`
Expected: Build succeeds with no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/config.ts frontend/vite.config.ts
git commit -m "feat: add BASE_PATH config and Vite base path support"
```

---

### Task 2: BASE_PATH — Router Basenames

**Files:**
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/pages/EmbedPage.tsx`

- [ ] **Step 1: Add basename to main router**

In `frontend/src/router.tsx`, import `BASE_PATH` and pass it to `createBrowserRouter`:

```typescript
import { BASE_PATH } from "@/config"

export const router = createBrowserRouter([
  // ... routes unchanged
], { basename: BASE_PATH || undefined })
```

- [ ] **Step 2: Add basename to embed router**

In `frontend/src/pages/EmbedPage.tsx`, import `BASE_PATH` and add basename:

```typescript
import { BASE_PATH } from "@/config"

const embedRouter = createBrowserRouter([
  // ... routes unchanged
], { basename: BASE_PATH || undefined })
```

- [ ] **Step 3: Strip BASE_PATH in App.tsx route detection**

In `frontend/src/App.tsx`, extract a helper to strip BASE_PATH from pathnames (used in both `getPublicPageComponent` and `App`):

```typescript
import { BASE_PATH } from "@/config"

/** Strip the deploy prefix (e.g. "/scout") so route matching works at any mount point. */
function stripBasePath(pathname: string): string {
  return BASE_PATH ? pathname.replace(new RegExp(`^${BASE_PATH}`), "") : pathname
}

function getPublicPageComponent(): React.ReactNode | null {
  const path = stripBasePath(window.location.pathname)
  // ... rest unchanged
}

export default function App() {
  // ... existing state
  const pathname = stripBasePath(window.location.pathname)
  const isPublicPage = /^\/shared\/(runs|threads)\/[^/]+\/?$/.test(pathname)
  const isEmbedPage = pathname.startsWith("/embed")
  // ... rest unchanged
```

- [ ] **Step 4: Strip BASE_PATH in useEmbedParams**

In `frontend/src/hooks/useEmbedParams.ts`, import `BASE_PATH` and strip it before checking the pathname:

```typescript
import { BASE_PATH } from "@/config"

export function useEmbedParams(): EmbedParams {
  return useMemo(() => {
    const params = new URLSearchParams(window.location.search)
    const pathname = window.location.pathname.replace(new RegExp(`^${BASE_PATH}`), "")
    const isEmbed = pathname.startsWith("/embed")
    // ... rest unchanged
  }, [])
}
```

- [ ] **Step 5: Verify build and lint**

Run: `cd frontend && bun run build && bun run lint`
Expected: Both pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/router.tsx frontend/src/pages/EmbedPage.tsx frontend/src/App.tsx frontend/src/hooks/useEmbedParams.ts
git commit -m "feat: add BASE_PATH basename to routers and route detection"
```

---

### Task 3: OAuth Popup Flow for Embed

When Scout is embedded in an iframe, full-page OAuth redirects fail because OAuth providers (e.g. CommCareHQ) set `X-Frame-Options` which blocks loading in an iframe. The solution: open OAuth in a popup window and detect when it completes.

**Files:**
- Modify: `frontend/src/components/LoginForm/LoginForm.tsx`
- Modify: `frontend/src/pages/EmbedPage.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add popup OAuth to LoginForm when embedded**

The LoginForm needs to detect embed mode and open OAuth in a popup instead of navigating. Import `useEmbedParams` and add popup logic:

```typescript
import { useEmbedParams } from "@/hooks/useEmbedParams"
import { BASE_PATH } from "@/config"

export function LoginForm() {
  const { isEmbed } = useEmbedParams()
  // ... existing state

  function handleOAuthClick(e: React.MouseEvent, provider: OAuthProvider) {
    if (!isEmbed) return // Let the <a> navigate normally

    e.preventDefault()
    const nextUrl = `${BASE_PATH}/embed/?popup_close=1`
    const authUrl = `${provider.login_url}?next=${encodeURIComponent(nextUrl)}`
    const popup = window.open(authUrl, "scout-oauth", "width=500,height=700")

    if (!popup) return

    // Poll for popup close — when it closes, re-fetch auth status
    const interval = setInterval(() => {
      if (!popup || popup.closed) {
        clearInterval(interval)
        fetchMe()
      }
    }, 500)
  }

  // ... in the JSX, update the OAuth button:
  // <a href={...} onClick={(e) => handleOAuthClick(e, provider)}>
```

Full updated OAuth button section:

```tsx
{providers.map((provider) => (
  <Button
    key={provider.id}
    variant="outline"
    className="w-full"
    asChild
    data-testid={`oauth-login-${provider.id}`}
  >
    <a
      href={`${provider.login_url}?next=/`}
      onClick={(e) => handleOAuthClick(e, provider)}
    >
      {provider.name}
    </a>
  </Button>
))}
```

Note: `fetchMe` needs to be pulled from the store in LoginForm. Add:
```typescript
const fetchMe = useAppStore((s) => s.authActions.fetchMe)
```

- [ ] **Step 2: Add `popup_close` auto-close in App.tsx**

When the OAuth flow completes, the popup redirects back to the app with `?popup_close=1`. If we detect this param and `window.opener` exists, close the popup immediately:

Add at the top of `App()` in `frontend/src/App.tsx`, before the existing hooks:

```typescript
// Auto-close OAuth popup after redirect
useEffect(() => {
  const params = new URLSearchParams(window.location.search)
  if (params.get("popup_close") === "1" && window.opener) {
    window.close()
  }
}, [])
```

- [ ] **Step 3: Add visibility-change re-auth in EmbedPage**

In `frontend/src/pages/EmbedPage.tsx`, the `fetchMe()` effect should also re-check auth when the iframe regains visibility (after the popup closes and the user returns to the parent tab). Update the existing fetchMe effect:

```typescript
useEffect(() => {
  fetchMe()

  // Re-check auth when the iframe regains visibility (e.g. after popup login).
  // Only re-fetch if we're not already authenticated — avoids re-triggering
  // the tenant setup chain on alt-tab.
  const handleVisibility = () => {
    if (
      document.visibilityState === "visible" &&
      useAppStore.getState().authStatus !== "authenticated"
    ) {
      fetchMe()
    }
  }
  document.addEventListener("visibilitychange", handleVisibility)
  return () => document.removeEventListener("visibilitychange", handleVisibility)
}, [fetchMe])
```

- [ ] **Step 4: Verify build and lint**

Run: `cd frontend && bun run build && bun run lint`
Expected: Both pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/LoginForm/LoginForm.tsx frontend/src/App.tsx frontend/src/pages/EmbedPage.tsx
git commit -m "feat: add popup OAuth flow for embed iframe authentication"
```

---

### Task 4: iframe Auto-Resize

**Files:**
- Create: `frontend/src/hooks/useAutoResize.ts`
- Modify: `frontend/src/components/EmbedLayout/EmbedLayout.tsx`
- Modify: `frontend/public/widget.js`

- [ ] **Step 1: Create `useAutoResize` hook**

Create `frontend/src/hooks/useAutoResize.ts`:

```typescript
import { useEffect, useRef } from "react"
import { useEmbedParams } from "./useEmbedParams"
import { useEmbedMessaging } from "./useEmbedMessaging"

/**
 * Observes document.documentElement and sends `scout:resize` to the parent
 * frame whenever scrollHeight changes. Uses requestAnimationFrame for
 * debouncing. Only active when running inside an iframe embed.
 */
export function useAutoResize() {
  const { isEmbed } = useEmbedParams()
  const { sendEvent } = useEmbedMessaging()
  const lastHeight = useRef(0)
  const rafId = useRef(0)

  useEffect(() => {
    if (!isEmbed || window.parent === window) return

    function reportHeight() {
      const height = document.documentElement.scrollHeight
      if (height !== lastHeight.current) {
        lastHeight.current = height
        sendEvent("scout:resize", { height })
      }
    }

    // Report initial height
    reportHeight()

    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(rafId.current)
      rafId.current = requestAnimationFrame(reportHeight)
    })

    observer.observe(document.documentElement)

    return () => {
      observer.disconnect()
      cancelAnimationFrame(rafId.current)
    }
  }, [isEmbed, sendEvent])
}
```

- [ ] **Step 2: Wire up `useAutoResize` in EmbedLayout and add min-height**

In `frontend/src/components/EmbedLayout/EmbedLayout.tsx`, import and call the hook, and add `min-h-[600px]`:

```typescript
import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { useEmbedParams } from "@/hooks/useEmbedParams"
import { useAutoResize } from "@/hooks/useAutoResize"

export function EmbedLayout() {
  const { mode } = useEmbedParams()
  const showSidebar = mode === "full"
  const showArtifacts = mode === "full" || mode === "chat+artifacts"

  useAutoResize()

  return (
    <div className="flex h-screen min-h-[600px]">
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

- [ ] **Step 3: Update widget.js with auto-resize handling, version, and absolute positioning**

Replace `frontend/public/widget.js` with the updated version that includes:
- `SCOUT_WIDGET_VERSION` constant
- `SCOUT_BASE` that includes the path prefix (not just origin)
- `scout:resize` message handling with `minHeight`
- Absolute positioning for the iframe (resolves `height:100%` issues with `min-height` ancestors)
- `version` property on `ScoutWidget`

Full updated `widget.js`:

```javascript
(function () {
  "use strict";

  var SCOUT_WIDGET_VERSION = "0.2.0";

  // Detect base URL from the script src, including any path prefix (e.g. /scout)
  var SCOUT_BASE = (function () {
    var scripts = document.getElementsByTagName("script");
    for (var i = 0; i < scripts.length; i++) {
      var src = scripts[i].src || "";
      if (src.indexOf("widget.js") !== -1) {
        var url = new URL(src);
        var basePath = url.pathname.replace(/\/widget\.js$/, "");
        return url.origin + basePath;
      }
    }
    return window.location.origin;
  })();

  // Origin-only for postMessage security checks
  var SCOUT_ORIGIN = new URL(SCOUT_BASE).origin;

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
    if (this.opts.provider) params.push("provider=" + encodeURIComponent(this.opts.provider));
    if (this.opts.theme) params.push("theme=" + encodeURIComponent(this.opts.theme));
    var src = SCOUT_BASE + "/embed/" + (params.length ? "?" + params.join("&") : "");

    // Create iframe — absolute positioning resolves height:100% correctly
    // even when ancestor elements use min-height instead of height
    this.iframe = document.createElement("iframe");
    this.iframe.src = src;
    this.iframe.style.cssText =
      "position:absolute;top:0;left:0;width:100%;height:100%;border:none;";
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
    this.container.style.position = "relative";
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

    if (data.type === "scout:resize" && typeof data.height === "number") {
      if (this.opts.autoResize !== false && this.container) {
        this.container.style.minHeight = data.height + "px";
      }
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
    version: SCOUT_WIDGET_VERSION,
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

- [ ] **Step 4: Verify build and lint**

Run: `cd frontend && bun run build && bun run lint`
Expected: Both pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useAutoResize.ts frontend/src/components/EmbedLayout/EmbedLayout.tsx frontend/public/widget.js
git commit -m "feat: add iframe auto-resize via ResizeObserver and postMessage"
```

---

### Task 5: Final Verification

- [ ] **Step 1: Run full frontend build**

Run: `cd frontend && bun run build`
Expected: Successful build with no TypeScript errors.

- [ ] **Step 2: Run frontend lint**

Run: `cd frontend && bun run lint`
Expected: No lint errors.

- [ ] **Step 3: Run backend tests**

Run: `uv run pytest`
Expected: All tests pass (no backend changes in this plan, but verify nothing is broken).

- [ ] **Step 4: Verify with `VITE_BASE_PATH=/scout`**

Run: `cd frontend && VITE_BASE_PATH=/scout bun run build`
Expected: Build succeeds. Check `frontend/dist/index.html` — asset paths should be prefixed with `/scout/`.
