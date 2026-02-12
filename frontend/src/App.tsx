import { useEffect } from "react"
import { RouterProvider } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { Skeleton } from "@/components/ui/skeleton"
import { router } from "@/router"
import { PublicRecipeRunPage } from "@/pages/PublicRecipeRunPage"

function getPublicPageComponent(): React.ReactNode | null {
  const path = window.location.pathname
  if (/^\/shared\/runs\/[^/]+\/?$/.test(path)) return <PublicRecipeRunPage />
  return null
}

export default function App() {
  const authStatus = useAppStore((s) => s.authStatus)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const isPublicPage = /^\/shared\/runs\/[^/]+\/?$/.test(window.location.pathname)

  useEffect(() => {
    if (!isPublicPage) {
      fetchMe()
    }
  }, [fetchMe, isPublicPage])

  if (isPublicPage) {
    return getPublicPageComponent()
  }

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

  return <RouterProvider router={router} />
}
