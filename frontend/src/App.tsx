import { useEffect } from "react"
import { RouterProvider } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { BASE_PATH } from "@/config"
import { NetworkStatusProvider } from "@/contexts/NetworkStatusContext"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { OnboardingWizard } from "@/components/OnboardingWizard/OnboardingWizard"
import { Skeleton } from "@/components/ui/skeleton"
import { router } from "@/router"
import { PublicRecipeRunPage } from "@/pages/PublicRecipeRunPage"
import { PublicThreadPage } from "@/pages/PublicThreadPage"
import { EmbedPage } from "@/pages/EmbedPage"

/** Strip the deploy prefix (e.g. "/scout") so route matching works at any mount point. */
function stripBasePath(pathname: string): string {
  return BASE_PATH && pathname.startsWith(BASE_PATH) ? pathname.slice(BASE_PATH.length) : pathname
}

function getPublicPageComponent(): React.ReactNode | null {
  const path = stripBasePath(window.location.pathname)
  if (/^\/shared\/runs\/[^/]+\/?$/.test(path)) return <PublicRecipeRunPage />
  if (/^\/shared\/threads\/[^/]+\/?$/.test(path)) return <PublicThreadPage />
  return null
}

export default function App() {
  const authStatus = useAppStore((s) => s.authStatus)
  const user = useAppStore((s) => s.user)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const pathname = stripBasePath(window.location.pathname)
  const isPublicPage = /^\/shared\/(runs|threads)\/[^/]+\/?$/.test(pathname)
  const isEmbedPage = pathname.startsWith("/embed")

  useEffect(() => {
    if (!isPublicPage && !isEmbedPage) {
      fetchMe()
    }
  }, [fetchMe, isPublicPage, isEmbedPage])

  // Auto-close OAuth popup after redirect.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (document.cookie.includes("scout_auth_popup=1") || params.get("popup_close") === "1") {
      document.cookie = "scout_auth_popup=;max-age=0;path=/"
      window.close()
    }
  }, [])

  if (isPublicPage) {
    return getPublicPageComponent()
  }

  if (isEmbedPage) {
    return <EmbedPage />
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

  // authenticated — check onboarding
  if (authStatus === "authenticated" && user && !user.onboarding_complete) {
    return <OnboardingWizard />
  }

  return (
    <NetworkStatusProvider>
      <RouterProvider router={router} />
    </NetworkStatusProvider>
  )
}
