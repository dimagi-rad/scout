import { useEffect } from "react"
import { RouterProvider } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { NetworkStatusProvider } from "@/contexts/NetworkStatusContext"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { OnboardingWizard } from "@/components/OnboardingWizard/OnboardingWizard"
import { Skeleton } from "@/components/ui/skeleton"
import { router } from "@/router"
import { PublicRecipeRunPage } from "@/pages/PublicRecipeRunPage"
import { PublicThreadPage } from "@/pages/PublicThreadPage"
import { EmbedPage } from "@/pages/EmbedPage"
import { BASE_PATH } from "@/config"

// Strip the base path prefix to get the app-relative path
function appPath(): string {
  const p = window.location.pathname
  return BASE_PATH && p.startsWith(BASE_PATH) ? p.slice(BASE_PATH.length) : p
}

function getPublicPageComponent(): React.ReactNode | null {
  const path = appPath()
  if (/^\/shared\/runs\/[^/]+\/?$/.test(path)) return <PublicRecipeRunPage />
  if (/^\/shared\/threads\/[^/]+\/?$/.test(path)) return <PublicThreadPage />
  return null
}

export default function App() {
  const authStatus = useAppStore((s) => s.authStatus)
  const user = useAppStore((s) => s.user)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const relPath = appPath()
  const isPublicPage = /^\/shared\/(runs|threads)\/[^/]+\/?$/.test(relPath)
  const isEmbedPage = relPath.startsWith("/embed")
  const isPopup = !!window.opener

  useEffect(() => {
    if (!isPublicPage && !isEmbedPage) {
      fetchMe()
    }
  }, [fetchMe, isPublicPage, isEmbedPage])

  // If opened as a popup (e.g. for OAuth from the embed widget), close
  // automatically once the user is authenticated so control returns to
  // the parent page.
  useEffect(() => {
    if (isPopup && authStatus === "authenticated") {
      window.close()
    }
  }, [isPopup, authStatus])

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
