import { useSyncExternalStore } from "react"
import { Hourglass } from "lucide-react"
import { busyTracker, type BusyTracker } from "@/api/busy"
import { Button } from "@/components/ui/button"

/** Calm, non-blocking notice for connection-limit "busy" answers; see api/busy. */
export function BusyNotice({ tracker = busyTracker }: { tracker?: BusyTracker }) {
  const { retrying, waiting } = useSyncExternalStore(tracker.subscribe, tracker.getSnapshot)
  const phase = retrying > 0 ? "retrying" : waiting > 0 ? "waiting" : null

  // Stays mounted while empty: screen readers skip a live region inserted along with its content.
  return (
    <div
      className="fixed bottom-4 left-1/2 z-40 w-96 max-w-[calc(100vw-2rem)] -translate-x-1/2"
      role="status"
      aria-live="polite"
      data-testid="busy-notice-region"
    >
      {phase && (
        <div
          className="flex items-start gap-3 rounded-lg border bg-card p-3 text-sm shadow-lg"
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
            {phase === "waiting" && (
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  onClick={tracker.retryAll}
                  data-testid="busy-notice-retry"
                >
                  Retry
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={tracker.dismissAll}
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
