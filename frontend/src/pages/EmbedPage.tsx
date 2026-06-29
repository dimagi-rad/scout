import { useEffect, useCallback } from "react"
import { RouterProvider, createBrowserRouter, Navigate } from "react-router-dom"
import { BASE_PATH } from "@/config"
import { useAppStore } from "@/store/store"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { Skeleton } from "@/components/ui/skeleton"
import { EmbedLayout } from "@/components/EmbedLayout/EmbedLayout"
import { ChatRoute } from "@/components/ChatPanel/ChatRoute"
import { ChatRedirect } from "@/components/ChatPanel/ChatRedirect"
import { ArtifactsPage } from "@/pages/ArtifactsPage"
import { DataDictionaryPage } from "@/pages/DataDictionaryPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { RecipesPage } from "@/pages/RecipesPage"
import { ConnectionsPage } from "@/pages/ConnectionsPage"
import { WorkspacesPage } from "@/pages/WorkspacesPage"
import { WorkspaceDetailPage } from "@/pages/WorkspaceDetailPage"
import { useEmbedMessaging } from "@/hooks/useEmbedMessaging"
import { useEmbedParams, type EmbedMode, type EmbedTheme } from "@/hooks/useEmbedParams"
import { EmbedSettingsProvider, useEmbedSettings } from "@/contexts/EmbedSettingsContext"

const embedRouter = createBrowserRouter([
  {
    path: "/embed",
    element: <EmbedLayout />,
    children: [
      { index: true, element: <ChatRedirect /> },
      { path: "chat", element: <ChatRedirect /> },
      { path: "workspaces/:workspaceId/chat", element: <ChatRoute /> },
      { path: "workspaces/:workspaceId/chat/:threadId", element: <ChatRoute /> },
      // Pretty chat URL: cosmetic slug + UUID. The uuid keeps the param name
      // `:workspaceId`; `:slug` is decorative and ignored for lookup. Bare
      // routes above stay for back-compat.
      { path: "workspaces/:slug/:workspaceId/chat", element: <ChatRoute /> },
      { path: "workspaces/:slug/:workspaceId/chat/:threadId", element: <ChatRoute /> },
      { path: "artifacts", element: <ArtifactsPage /> },
      { path: "knowledge", element: <KnowledgePage /> },
      { path: "knowledge/new", element: <KnowledgePage /> },
      { path: "knowledge/:id", element: <KnowledgePage /> },
      { path: "recipes", element: <RecipesPage /> },
      { path: "recipes/:id", element: <RecipesPage /> },
      { path: "recipes/:id/runs/:runId", element: <RecipesPage /> },
      { path: "data-dictionary", element: <DataDictionaryPage /> },
      { path: "settings/connections", element: <ConnectionsPage /> },
      { path: "workspaces", element: <WorkspacesPage /> },
      { path: "workspaces/:workspaceId", element: <WorkspaceDetailPage /> },
      // Pretty URL: cosmetic slug + UUID. Resolution is always by :workspaceId.
      { path: "workspaces/:slug/:workspaceId", element: <WorkspaceDetailPage /> },
      { path: "*", element: <Navigate to="/embed" replace /> },
    ],
  },
], { basename: BASE_PATH || undefined })

export function EmbedPage() {
  const { mode, theme } = useEmbedParams()
  return (
    <EmbedSettingsProvider initialMode={mode} initialTheme={theme}>
      <EmbedApp />
    </EmbedSettingsProvider>
  )
}

function EmbedApp() {
  const authStatus = useAppStore((s) => s.authStatus)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const ensureTenant = useAppStore((s) => s.domainActions.ensureTenant)
  const { tenant, provider } = useEmbedParams()
  const { setMode, setTheme } = useEmbedSettings()

  const handleCommand = useCallback((type: string, payload: Record<string, unknown>) => {
    if (type === "scout:set-tenant") {
      const tenantId = payload.tenant as string
      const prov = (payload.provider as string) || "commcare_connect"
      if (tenantId) {
        ensureTenant(prov, tenantId)
      }
    }
    if (type === "scout:set-mode") {
      // issue #248, 06#6: apply requested mode/theme live, don't just log it.
      if (typeof payload.mode === "string") {
        setMode(payload.mode as EmbedMode)
      }
      if (typeof payload.theme === "string") {
        setTheme(payload.theme as EmbedTheme)
      }
    }
    if (type === "scout:set-theme" && typeof payload.theme === "string") {
      setTheme(payload.theme as EmbedTheme)
    }
  }, [ensureTenant, setMode, setTheme])

  const { sendEvent } = useEmbedMessaging(handleCommand)

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

  useEffect(() => {
    if (authStatus === "authenticated") {
      sendEvent("scout:ready")
    } else if (authStatus === "unauthenticated") {
      sendEvent("scout:auth-required")
    }
  }, [authStatus, sendEvent])

  useEffect(() => {
    if (authStatus === "authenticated" && tenant) {
      ensureTenant(provider, tenant)
    }
  }, [authStatus, tenant, provider, ensureTenant])

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
