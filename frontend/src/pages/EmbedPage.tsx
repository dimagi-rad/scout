import { useEffect, useCallback } from "react"
import { RouterProvider, createBrowserRouter } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { Skeleton } from "@/components/ui/skeleton"
import { EmbedLayout } from "@/components/EmbedLayout/EmbedLayout"
import { ChatPanel } from "@/components/ChatPanel/ChatPanel"
import { ArtifactsPage } from "@/pages/ArtifactsPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { RecipesPage } from "@/pages/RecipesPage"
import { useEmbedMessaging } from "@/hooks/useEmbedMessaging"

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

  const handleCommand = useCallback((type: string, payload: Record<string, unknown>) => {
    if (type === "scout:set-tenant") {
      console.log("[Scout Embed] set-tenant:", payload.tenant)
    }
    if (type === "scout:set-mode") {
      console.log("[Scout Embed] set-mode:", payload.mode)
    }
  }, [])

  const { sendEvent } = useEmbedMessaging(handleCommand)

  useEffect(() => {
    fetchMe()
  }, [fetchMe])

  useEffect(() => {
    if (authStatus === "authenticated") {
      sendEvent("scout:ready")
    } else if (authStatus === "unauthenticated") {
      sendEvent("scout:auth-required")
    }
  }, [authStatus, sendEvent])

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
