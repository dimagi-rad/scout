# Offline Indicator Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show a global offline banner when the dev server is unreachable, suppress per-page error messages while offline, and auto-recover when the server comes back.

**Architecture:** A React context (`NetworkStatusContext`) provides online/offline status app-wide by polling `GET /health/` every 5 seconds and using `navigator.onLine` for fast detection. An `OfflineBanner` component reads from this context and slides in from the bottom. On reconnect, any store slices in `"error"` state are automatically re-fetched. Per-page error UIs are suppressed while offline (since the banner already explains why).

**Tech Stack:** React 19, Zustand, Tailwind CSS 4, lucide-react, shadcn/ui design tokens

---

### Task 1: NetworkStatusContext and useNetworkStatus hook

**Files:**
- Create: `frontend/src/contexts/NetworkStatusContext.tsx`
- Create: `frontend/src/hooks/useNetworkStatus.ts`

**Step 1: Create the context and provider**

Create `frontend/src/contexts/NetworkStatusContext.tsx`:

```tsx
import { createContext, useContext, useEffect, useRef, useState } from "react"

export type NetworkStatus = "online" | "offline" | "reconnecting"

interface NetworkStatusContextValue {
  isOnline: boolean
  status: NetworkStatus
}

const NetworkStatusContext = createContext<NetworkStatusContextValue>({
  isOnline: true,
  status: "online",
})

const POLL_INTERVAL = 5000
const RECONNECT_DISPLAY_MS = 2000

export function NetworkStatusProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<NetworkStatus>("online")
  const wasOfflineRef = useRef(false)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    let polling = true

    async function checkHealth() {
      if (!polling) return
      try {
        const res = await fetch("/health/", { method: "GET", cache: "no-store" })
        if (res.ok) {
          if (wasOfflineRef.current) {
            wasOfflineRef.current = false
            setStatus("reconnecting")
            reconnectTimerRef.current = setTimeout(() => {
              setStatus("online")
            }, RECONNECT_DISPLAY_MS)
          } else {
            setStatus("online")
          }
        } else {
          throw new Error("not ok")
        }
      } catch {
        wasOfflineRef.current = true
        setStatus("offline")
      }
    }

    // Fast first check via navigator.onLine
    if (!navigator.onLine) {
      wasOfflineRef.current = true
      setStatus("offline")
    }

    checkHealth()
    const interval = setInterval(checkHealth, POLL_INTERVAL)

    const handleOnline = () => checkHealth()
    const handleOffline = () => {
      wasOfflineRef.current = true
      setStatus("offline")
    }

    window.addEventListener("online", handleOnline)
    window.addEventListener("offline", handleOffline)

    return () => {
      polling = false
      clearInterval(interval)
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      window.removeEventListener("online", handleOnline)
      window.removeEventListener("offline", handleOffline)
    }
  }, [])

  return (
    <NetworkStatusContext.Provider value={{ isOnline: status === "online" || status === "reconnecting", status }}>
      {children}
    </NetworkStatusContext.Provider>
  )
}

export function useNetworkStatus() {
  return useContext(NetworkStatusContext)
}
```

**Step 2: Create the hook re-export**

Create `frontend/src/hooks/useNetworkStatus.ts`:

```ts
export { useNetworkStatus } from "@/contexts/NetworkStatusContext"
```

**Step 3: Verify TypeScript compiles**

```bash
cd frontend && bun run build 2>&1 | head -30
```

Expected: no TypeScript errors related to the new files (build may fail on other things, that's fine for now).

**Step 4: Commit**

```bash
git add frontend/src/contexts/NetworkStatusContext.tsx frontend/src/hooks/useNetworkStatus.ts
git commit -m "feat: add NetworkStatusContext with health polling"
```

---

### Task 2: OfflineBanner component

**Files:**
- Create: `frontend/src/components/OfflineBanner/OfflineBanner.tsx`

**Step 1: Create the component**

Create `frontend/src/components/OfflineBanner/OfflineBanner.tsx`:

```tsx
import { Wifi, WifiOff } from "lucide-react"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"

export function OfflineBanner() {
  const { status } = useNetworkStatus()

  if (status === "online") return null

  return (
    <div
      className={`fixed bottom-0 left-0 right-0 z-50 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium transition-all duration-300 ${
        status === "reconnecting"
          ? "bg-green-600 text-white"
          : "bg-destructive text-destructive-foreground"
      }`}
      role="status"
      aria-live="polite"
      data-testid="offline-banner"
    >
      {status === "reconnecting" ? (
        <>
          <Wifi className="h-4 w-4" />
          <span>Reconnected</span>
        </>
      ) : (
        <>
          <WifiOff className="h-4 w-4 animate-pulse" />
          <span>Server unreachable — attempting to reconnect...</span>
        </>
      )}
    </div>
  )
}
```

**Step 2: Verify TypeScript compiles**

```bash
cd frontend && bun run build 2>&1 | head -30
```

Expected: no errors in the new file.

**Step 3: Commit**

```bash
git add frontend/src/components/OfflineBanner/OfflineBanner.tsx
git commit -m "feat: add OfflineBanner component"
```

---

### Task 3: Wire NetworkStatusProvider and OfflineBanner into the app + auto-retry on reconnect

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/AppLayout/AppLayout.tsx`

**Step 1: Wrap RouterProvider with NetworkStatusProvider in App.tsx**

In `frontend/src/App.tsx`, add the import and wrap the `RouterProvider`:

Current last line:
```tsx
  return <RouterProvider router={router} />
```

Replace with:
```tsx
import { NetworkStatusProvider } from "@/contexts/NetworkStatusContext"

  return (
    <NetworkStatusProvider>
      <RouterProvider router={router} />
    </NetworkStatusProvider>
  )
```

Full import addition at top of file (add after existing imports):
```tsx
import { NetworkStatusProvider } from "@/contexts/NetworkStatusContext"
```

**Step 2: Add OfflineBanner and auto-retry to AppLayout**

Replace the entire contents of `frontend/src/components/AppLayout/AppLayout.tsx` with:

```tsx
import { useEffect, useRef } from "react"
import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { OfflineBanner } from "@/components/OfflineBanner/OfflineBanner"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
import { useAppStore } from "@/store/store"

export function AppLayout() {
  const { isOnline } = useNetworkStatus()
  const prevIsOnlineRef = useRef(isOnline)

  const dictionaryStatus = useAppStore((s) => s.dictionaryStatus)
  const recipeStatus = useAppStore((s) => s.recipeStatus)
  const knowledgeStatus = useAppStore((s) => s.knowledgeStatus)
  const artifactsStatus = useAppStore((s) => s.artifactsStatus)

  const fetchDictionary = useAppStore((s) => s.dictionaryActions.fetchDictionary)
  const fetchRecipes = useAppStore((s) => s.recipeActions.fetchRecipes)
  const fetchKnowledge = useAppStore((s) => s.knowledgeActions.fetchKnowledge)
  const fetchArtifacts = useAppStore((s) => s.artifactActions.fetchArtifacts)

  // Auto-retry errored slices when we come back online
  useEffect(() => {
    const wasOffline = !prevIsOnlineRef.current
    prevIsOnlineRef.current = isOnline

    if (isOnline && wasOffline) {
      if (dictionaryStatus === "error") fetchDictionary()
      if (recipeStatus === "error") fetchRecipes()
      if (knowledgeStatus === "error") fetchKnowledge({})
      if (artifactsStatus === "error") fetchArtifacts()
    }
  }, [
    isOnline,
    dictionaryStatus,
    recipeStatus,
    knowledgeStatus,
    artifactsStatus,
    fetchDictionary,
    fetchRecipes,
    fetchKnowledge,
    fetchArtifacts,
  ])

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 min-w-0 overflow-auto">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
      <ArtifactPanel />
      <OfflineBanner />
    </div>
  )
}
```

> **Note on store actions:** Check the exact action names in the store slices before implementing:
> - `knowledgeActions.fetchKnowledge` — check `knowledgeSlice.ts` for the exact function name and signature
> - `artifactActions.fetchArtifacts` — check `artifactSlice.ts` for the exact function name

**Step 3: Verify TypeScript compiles cleanly**

```bash
cd frontend && bun run build 2>&1 | head -50
```

Expected: clean build. Fix any type errors before proceeding.

**Step 4: Commit**

```bash
git add frontend/src/App.tsx frontend/src/components/AppLayout/AppLayout.tsx
git commit -m "feat: mount OfflineBanner in AppLayout with auto-retry on reconnect"
```

---

### Task 4: Suppress per-page error states while offline

**Files:**
- Modify: `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx`
- Modify: `frontend/src/pages/RecipesPage/RecipesPage.tsx`
- Modify: `frontend/src/pages/KnowledgePage/KnowledgePage.tsx`
- Modify: `frontend/src/pages/ArtifactsPage/ArtifactsPage.tsx`

**Step 1: Update DataDictionaryPage error state**

In `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx`:

Add import at top:
```tsx
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
```

Add inside the component (after existing hook calls):
```tsx
const { isOnline } = useNetworkStatus()
```

Replace the error block (lines 53–68) with:
```tsx
  // Error state — only show if we're online (offline handled by global banner)
  if (dictionaryStatus === "error" && isOnline) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <Database className="mx-auto h-12 w-12 text-muted-foreground" />
          <h2 className="mt-4 text-lg font-medium">Failed to load dictionary</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            There was an error loading the data dictionary
          </p>
          <Button onClick={() => fetchDictionary()} className="mt-4">
            Try Again
          </Button>
        </div>
      </div>
    )
  }
```

**Step 2: Update RecipesPage error state**

In `frontend/src/pages/RecipesPage/RecipesPage.tsx`:

Add import:
```tsx
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
```

Add inside component:
```tsx
const { isOnline } = useNetworkStatus()
```

Replace the error block (lines 227–231):
```tsx
      {/* Error state — only show if we're online */}
      {recipeStatus === "error" && isOnline && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
          Failed to load recipes. Please try again.
        </div>
      )}
```

**Step 3: Update KnowledgePage error state**

Find `frontend/src/pages/KnowledgePage/KnowledgePage.tsx`. Add `useNetworkStatus` import and hook call, then add `&& isOnline` to the `knowledgeStatus === "error"` condition.

**Step 4: Update ArtifactsPage error state**

Find `frontend/src/pages/ArtifactsPage/ArtifactsPage.tsx`. Add `useNetworkStatus` import and hook call, then add `&& isOnline` to the `artifactsStatus === "error"` condition.

**Step 5: Verify the full build passes**

```bash
cd frontend && bun run build
```

Expected: exit 0, no TypeScript errors.

**Step 6: Run the linter**

```bash
cd frontend && bun run lint
```

Expected: no errors. Fix any lint issues before committing.

**Step 7: Commit**

```bash
git add frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx \
        frontend/src/pages/RecipesPage/RecipesPage.tsx \
        frontend/src/pages/KnowledgePage/KnowledgePage.tsx \
        frontend/src/pages/ArtifactsPage/ArtifactsPage.tsx
git commit -m "feat: suppress per-page errors while offline"
```

---

### Task 5: Manual smoke test

Start the dev servers and verify behavior:

```bash
uv run honcho -f Procfile.dev start
```

**Test offline behavior:**
1. Open the app at http://localhost:5173
2. Stop the Django server (Ctrl+C on the Django process, or kill port 8000)
3. Within ~5 seconds: banner slides in at bottom — "Server unreachable — attempting to reconnect..."
4. Navigate to Recipes, Data Dictionary, Knowledge — no per-page error messages shown
5. You can still click around the sidebar freely

**Test reconnection:**
1. Restart the Django server
2. Within ~5 seconds: banner briefly shows "Reconnected" in green
3. Banner auto-dismisses after ~2 seconds
4. Any pages that were in error state re-fetch and display their data

**Test real server errors (server is up but returns error):**
1. Verify per-page error states still show for actual server errors (not network errors)

If all three scenarios work correctly, the feature is complete.
