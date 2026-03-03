import { useEffect, useCallback, useRef } from "react"
import { RouterProvider, createBrowserRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { Skeleton } from "@/components/ui/skeleton"
import { Loader2 } from "lucide-react"
import { EmbedLayout } from "@/components/EmbedLayout/EmbedLayout"
import { ChatPanel } from "@/components/ChatPanel/ChatPanel"
import { ArtifactsPage } from "@/pages/ArtifactsPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { RecipesPage } from "@/pages/RecipesPage"
import { useEmbedMessaging } from "@/hooks/useEmbedMessaging"
import { useEmbedParams } from "@/hooks/useEmbedParams"
import { BASE_PATH } from "@/config"

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
], { basename: BASE_PATH || undefined })

export function EmbedPage() {
  const authStatus = useAppStore((s) => s.authStatus)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const ensureTenant = useAppStore((s) => s.domainActions.ensureTenant)
  const ensureWorkspaceForTenant = useAppStore(
    (s) => s.workspaceActions.ensureWorkspaceForTenant,
  )
  const workspaceSwitching = useAppStore((s) => s.workspaceSwitching)
  const { tenant, provider } = useEmbedParams()

  const setWorkspaceSwitching = useAppStore((s) => s.workspaceActions.setWorkspaceSwitching)

  const handleCommand = useCallback((type: string, payload: Record<string, unknown>) => {
    if (type === "scout:set-tenant") {
      const tenantId = payload.tenant as string
      const prov = (payload.provider as string) || "commcare_connect"
      if (tenantId) {
        setWorkspaceSwitching(true)
        ensureTenant(prov, tenantId).then(() => ensureWorkspaceForTenant(tenantId))
      }
    }
    if (type === "scout:set-mode") {
      console.log("[Scout Embed] set-mode:", payload.mode)
    }
  }, [ensureTenant, ensureWorkspaceForTenant, setWorkspaceSwitching])

  const { sendEvent } = useEmbedMessaging(handleCommand)

  useEffect(() => {
    fetchMe()

    // Re-check auth when the iframe regains visibility (e.g. after popup login).
    // Only re-fetch if we're not already authenticated — avoids re-triggering
    // the tenant setup chain (and the "Switching workspace" overlay) on alt-tab.
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

  useEffect(() => {
    if (authStatus === "authenticated") {
      sendEvent("scout:ready")
    } else if (authStatus === "unauthenticated") {
      sendEvent("scout:auth-required")
    }
  }, [authStatus, sendEvent])

  // Force a browser repaint when the switching overlay clears.
  // Browsers can skip repaints in cross-origin iframes; this nudge
  // ensures the overlay removal is visually flushed.
  const wrapperRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!workspaceSwitching && wrapperRef.current) {
      const el = wrapperRef.current
      el.style.transform = "translateZ(0)"
      requestAnimationFrame(() => { el.style.transform = "" })
    }
  }, [workspaceSwitching])

  // Auto-select tenant from URL param after authentication (run once)
  const tenantSetupDone = useRef(false)
  useEffect(() => {
    if (authStatus === "authenticated" && tenant && !tenantSetupDone.current) {
      tenantSetupDone.current = true
      setWorkspaceSwitching(true)
      ensureTenant(provider, tenant).then(() => ensureWorkspaceForTenant(tenant))
    }
  }, [authStatus, tenant, provider, ensureTenant, ensureWorkspaceForTenant, setWorkspaceSwitching])

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
    // Don't render OAuth links inside the iframe — they can't navigate to
    // external OAuth providers due to X-Frame-Options restrictions.
    // The parent page handles auth via a popup (scout:auth-required event).
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="text-center text-muted-foreground">
          <p className="text-sm">Authentication required</p>
        </div>
      </div>
    )
  }

  return (
    <div ref={wrapperRef} className="relative h-screen">
      <RouterProvider router={embedRouter} />
      {workspaceSwitching && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-background/80">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading workspace…
          </div>
        </div>
      )}
    </div>
  )
}
