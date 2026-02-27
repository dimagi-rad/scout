import { useEffect } from "react"
import { RouterProvider } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { OnboardingWizard } from "@/components/OnboardingWizard/OnboardingWizard"
import { Skeleton } from "@/components/ui/skeleton"
import { router } from "@/router"
import { PublicRecipeRunPage } from "@/pages/PublicRecipeRunPage"
import { PublicThreadPage } from "@/pages/PublicThreadPage"
import { EmbedPage } from "@/pages/EmbedPage"

function getPublicPageComponent(): React.ReactNode | null {
  const path = window.location.pathname
  if (/^\/shared\/runs\/[^/]+\/?$/.test(path)) return <PublicRecipeRunPage />
  if (/^\/shared\/threads\/[^/]+\/?$/.test(path)) return <PublicThreadPage />
  return null
}

export default function App() {
  const authStatus = useAppStore((s) => s.authStatus)
  const user = useAppStore((s) => s.user)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const isPublicPage = /^\/shared\/(runs|threads)\/[^/]+\/?$/.test(window.location.pathname)
  const isEmbedPage = window.location.pathname.startsWith("/embed")

  useEffect(() => {
    if (!isPublicPage && !isEmbedPage) {
      fetchMe()
    }
  }, [fetchMe, isPublicPage, isEmbedPage])

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

  return <RouterProvider router={router} />
}
