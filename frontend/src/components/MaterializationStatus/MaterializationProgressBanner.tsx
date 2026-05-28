import { Square, Loader2 } from "lucide-react"
import { useState } from "react"
import { api } from "@/api/client"
import type { ActiveJob } from "@/api/jobs"

interface Props {
  job: ActiveJob
  workspaceId: string
}

/**
 * Full-width sticky banner that appears at the bottom of the chat thread
 * whenever a materialization job is active. Designed to be impossible to miss:
 * distinct background, large text, and a prominent Stop button.
 *
 * Rendered by ChatPanel below the message list and above the input area.
 * The inline tool-call card still exists for history; this banner is the
 * primary real-time surface.
 */
export function MaterializationProgressBanner({ job, workspaceId }: Props) {
  const [cancelState, setCancelState] = useState<"idle" | "pending" | "error">("idle")

  const handleCancel = async (e: React.MouseEvent) => {
    e.preventDefault()
    if (cancelState === "pending") return
    setCancelState("pending")
    try {
      await api.post(
        `/api/workspaces/${workspaceId}/jobs/${job.thread_job_id}/cancel/`,
        {},
      )
    } catch {
      setCancelState("error")
      setTimeout(() => setCancelState("idle"), 3000)
    }
  }

  const progress = job.progress
  const rowsLoaded = progress?.rows_loaded ?? 0
  const rowsTotal = progress?.rows_total ?? null
  const percent = progress?.percent ?? null
  const sourceName = progress?.source ?? null
  const step = progress?.step ?? null
  const totalSteps = progress?.total_steps ?? null

  // Build the progress description
  let progressText: string
  if (rowsLoaded > 0) {
    const rowsStr = rowsLoaded.toLocaleString()
    if (rowsTotal != null && rowsTotal > 0) {
      progressText = `${rowsStr} / ${rowsTotal.toLocaleString()} rows`
    } else {
      progressText = `${rowsStr} rows fetched`
    }
  } else if (sourceName) {
    progressText = `Loading ${sourceName}…`
  } else {
    progressText = "Preparing…"
  }

  const stepText = step != null && totalSteps != null ? `Step ${step} of ${totalSteps}` : null

  return (
    <div
      className="border-t-2 border-primary/30 bg-primary/5 px-4 py-3 flex items-center gap-3"
      data-testid="materialization-progress-banner"
      role="status"
      aria-live="polite"
    >
      {/* Spinner */}
      <Loader2
        className="h-5 w-5 shrink-0 animate-spin text-primary"
        aria-hidden="true"
      />

      {/* Progress text block */}
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span
            className="font-semibold text-sm text-foreground"
            data-testid="materialization-banner-source"
          >
            {sourceName ? `Fetching ${sourceName}` : "Materializing data"}
          </span>
          {percent != null && (
            <span
              className="text-sm font-bold text-primary"
              data-testid="materialization-banner-percent"
            >
              {percent}%
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 mt-0.5 flex-wrap">
          <span
            className="text-xs text-muted-foreground"
            data-testid="materialization-banner-rows"
          >
            {progressText}
          </span>
          {stepText && (
            <span className="text-xs text-muted-foreground opacity-70">
              · {stepText}
            </span>
          )}
        </div>
        {/* Progress bar (only shown when percent is known) */}
        {percent != null && (
          <div
            className="mt-1.5 h-1.5 rounded-full bg-primary/20 overflow-hidden"
            data-testid="materialization-banner-progress-bar"
          >
            <div
              className="h-full rounded-full bg-primary transition-all duration-500 ease-out"
              style={{ width: `${Math.min(percent, 100)}%` }}
            />
          </div>
        )}
      </div>

      {/* Stop button — prominent, clearly labeled */}
      <button
        type="button"
        onClick={handleCancel}
        disabled={cancelState === "pending"}
        className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium border transition-colors shrink-0 ${
          cancelState === "error"
            ? "border-red-500 bg-red-500/10 text-red-600"
            : cancelState === "pending"
              ? "border-border bg-muted text-muted-foreground cursor-not-allowed"
              : "border-red-500/50 bg-red-500/10 text-red-600 hover:bg-red-500/20 hover:border-red-500"
        }`}
        data-testid="materialization-banner-stop-btn"
        title={
          cancelState === "error"
            ? "Cancel failed — try again"
            : cancelState === "pending"
              ? "Cancelling…"
              : "Stop data loading"
        }
      >
        <Square
          className={`h-3.5 w-3.5 ${cancelState === "pending" ? "animate-pulse" : ""}`}
          aria-hidden="true"
        />
        <span>
          {cancelState === "pending"
            ? "Stopping…"
            : cancelState === "error"
              ? "Stop failed"
              : "Stop"}
        </span>
      </button>
    </div>
  )
}
