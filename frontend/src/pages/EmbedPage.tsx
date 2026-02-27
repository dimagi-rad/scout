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
  // tenant param available via useEmbedParams() for future multi-tenant support
  useEmbedParams()

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
