import { useSyncExternalStore } from "react"
import { Hourglass } from "lucide-react"
import { busyTracker, type BusyTracker } from "@/api/busy"
import { Button } from "@/components/ui/button"

function busyPhase(retrying: number, stillBusy: boolean): "retrying" | "still-busy" | null {
  if (retrying > 0) return "retrying"
  if (stillBusy) return "still-busy"
  return null
}

interface BusyNoticeProps {
  tracker?: BusyTracker
  /** Re-runs everything the page loads; the requests that gave up have already failed. */
  onRetry?: () => void
}

/** Calm, non-blocking notice for connection-limit "busy" answers; see api/busy. */
export function BusyNotice({
  tracker = busyTracker,
  onRetry = () => window.location.reload(),
}: BusyNoticeProps) {
  const { retrying, stillBusy } = useSyncExternalStore(tracker.subscribe, tracker.getSnapshot)
  const phase = busyPhase(retrying, stillBusy)

  // Stays mounted while empty: screen readers skip a live region inserted along with its content.
  // Above modal overlays (z-50), with its own pointer events, so a dialog whose save
  // came back busy never hides the way out; bottom-16 clears the OfflineBanner.
  return (
    <div
      className="pointer-events-none fixed bottom-16 left-1/2 z-[60] w-96 max-w-[calc(100vw-2rem)] -translate-x-1/2"
      role="status"
      aria-live="polite"
      data-testid="busy-notice-region"
    >
      {phase && (
        <div
          className="pointer-events-auto flex items-start gap-3 rounded-lg border bg-card p-3 text-sm shadow-lg"
          data-testid="busy-notice"
          data-phase={phase}
        >
          <Hourglass className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0 flex-1 space-y-2">
            <p data-testid="busy-notice-message">
              {phase === "retrying"
                ? "Scout is busy — retrying…"
                : "Scout is still busy. Please try again in a few seconds."}
            </p>
            {phase === "still-busy" && (
              <div className="flex gap-2">
                <Button type="button" size="sm" onClick={onRetry} data-testid="busy-notice-retry">
                  Retry
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={tracker.recovered}
                  data-testid="busy-notice-dismiss"
                >
                  Dismiss
                </Button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
