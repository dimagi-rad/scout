import { useState } from "react"
import { Link } from "react-router-dom"
import { Clock, RotateCw, X } from "lucide-react"
import { jobsApi } from "@/api/jobs"
import { workspaceApi } from "@/api/workspaces"
import { useRetryableAction } from "@/hooks/useRetryableAction"
import { useRefetchOnLoadEnd } from "@/hooks/useRefetchOnLoadEnd"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"
import { CONNECTIONS_PATH } from "@/lib/routes"
import { dismissStaleBanner, isStaleBannerDismissed, staleData } from "./staleData"

const REFRESH_FAILED = "Refresh failed — try again"
export const READ_ONLY_REFRESH_NOTE = "A workspace member with write access can refresh it."

interface Props {
  workspaceId: string
  /** A load is running: the banner hides, and refetches freshness when it ends. */
  loading?: boolean
  onRefreshStarted?: () => void
}

/**
 * Offers a manual refresh once the workspace's oldest serving source is past
 * the server's threshold (#173: refreshing stays manual). Mount with
 * `key={workspaceId}` so dismissal and fetched detail belong to one workspace.
 */
export function StaleDataBanner({ workspaceId, loading = false, onRefreshStarted }: Props) {
  const freshness = useRefetchOnLoadEnd(workspaceApi.getFreshness, workspaceId, loading)
  const { canWrite } = useWorkspaceRole(workspaceId)
  const [dismissed, setDismissed] = useState(() => isStaleBannerDismissed(workspaceId))
  const refresh = useRetryableAction(REFRESH_FAILED, canWrite)

  const stale = dismissed ? null : staleData(freshness, { loading })
  if (!stale) return null

  const handleRefresh = async () => {
    if (refresh.blocked) return
    const ok = await refresh.run(() => jobsApi.retryMaterialization(workspaceId, {}))
    if (!ok) return
    onRefreshStarted?.()
    // Stay disabled until the job poll sees the load and hides this banner.
    refresh.settle(5000)
  }

  const handleDismiss = () => {
    dismissStaleBanner(workspaceId)
    setDismissed(true)
  }

  const subject = stale.oldestSourceName ? `${stale.oldestSourceName}'s data` : "This data"
  const reconnect = stale.reconnectProviders
  let callToAction = canWrite ? "Refresh it now?" : READ_ONLY_REFRESH_NOTE
  if (reconnect.length > 0) {
    callToAction = `Your ${reconnect.join(" and ")} sign-in expired, so a refresh can't fetch it.`
  }

  return (
    <div className="px-4 pt-1 pb-2" data-testid="stale-data-banner">
      <div
        role="status"
        className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-amber-600/40 bg-amber-50 px-4 py-2.5 text-sm text-amber-900 dark:border-amber-400/30 dark:bg-amber-950/40 dark:text-amber-100"
      >
        <Clock className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        <p className="min-w-[12rem] flex-1" data-testid="stale-data-banner-message">
          {subject} was last refreshed {stale.ageLabel}.{" "}
          {callToAction}
        </p>
        {refresh.failure && (
          <p
            role="alert"
            className="basis-full text-xs text-red-600 dark:text-red-400"
            data-testid="stale-data-banner-error"
          >
            {refresh.failure.message}
          </p>
        )}
        <div className="ml-auto flex items-center gap-2">
          {reconnect.length > 0 ? (
            reconnect.map((provider) => (
              <Link
                key={provider}
                to={CONNECTIONS_PATH}
                className="rounded-md border border-amber-600/40 px-2.5 py-1 text-xs font-medium hover:bg-amber-500/10"
                data-testid="stale-data-banner-reconnect"
              >
                Reconnect {provider}
              </Link>
            ))
          ) : (
            canWrite && (
              <button
                type="button"
                onClick={handleRefresh}
                disabled={refresh.blocked}
                className="flex items-center gap-1 rounded-md border border-amber-600/40 px-2.5 py-1 text-xs font-medium hover:bg-amber-500/10 disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="stale-data-banner-refresh"
              >
                <RotateCw
                  className={`h-3 w-3 ${refresh.state === "pending" ? "animate-spin" : ""}`}
                />
                {refresh.state === "pending" ? "Starting…" : "Refresh"}
              </button>
            )
          )}
          <button
            type="button"
            onClick={handleDismiss}
            aria-label="Dismiss"
            title="Dismiss"
            className="rounded-md p-1 hover:bg-amber-500/10"
            data-testid="stale-data-banner-dismiss"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  )
}
