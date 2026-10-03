import { useEffect } from "react"
import { RouterProvider } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { BASE_PATH } from "@/config"
import { NetworkStatusProvider } from "@/contexts/NetworkStatusContext"
import { BusyNotice } from "@/components/BusyNotice/BusyNotice"
import { LoginForm } from "@/components/LoginForm/LoginForm"
import { OnboardingWizard } from "@/components/OnboardingWizard/OnboardingWizard"
import { Skeleton } from "@/components/ui/skeleton"
import { router } from "@/router"
import { EmbedPage } from "@/pages/EmbedPage"
import { setSentryUser } from "@/lib/sentry"
import { reportBoundaryError } from "@/lib/reportRenderError"

/** Strip the deploy prefix (e.g. "/scout") so route matching works at any mount point. */
function stripBasePath(pathname: string): string {
  return BASE_PATH && pathname.startsWith(BASE_PATH) ? pathname.slice(BASE_PATH.length) : pathname
}

export default function App() {
  return (
    <>
      <AppContent />
      <BusyNotice />
    </>
  )
}

function AppContent() {
  const authStatus = useAppStore((s) => s.authStatus)
  const user = useAppStore((s) => s.user)
  const fetchMe = useAppStore((s) => s.authActions.fetchMe)
  const pathname = stripBasePath(window.location.pathname)
  const isEmbedPage = pathname.startsWith("/embed")

  useEffect(() => {
    if (!isEmbedPage) {
      fetchMe()
    }
  }, [fetchMe, isEmbedPage])

  const userId = user?.id
  useEffect(() => setSentryUser(userId), [userId])

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
    <NetworkStatusProvider key={user?.id}>
      {/* Each route's default error element only logs, so layout crashes outside
          AppLayout's own boundary would otherwise go unreported. */}
      <RouterProvider router={router} onError={(error) => reportBoundaryError(error)} />
    </NetworkStatusProvider>
  )
}
