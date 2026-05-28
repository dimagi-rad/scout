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
 * solid amber background that pops against the neutral chat palette, large
 * percentage counter, thick progress bar, and a solid red Stop button with
 * maximum contrast.
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

  // Build the count display: "27,000 / 50,000 (54%)" or "27,000 rows"
  let countText: string
  if (rowsLoaded > 0) {
    const rowsStr = rowsLoaded.toLocaleString()
    if (rowsTotal != null && rowsTotal > 0 && percent != null) {
      countText = `${rowsStr} / ${rowsTotal.toLocaleString()} (${percent}%)`
    } else if (rowsTotal != null && rowsTotal > 0) {
      countText = `${rowsStr} / ${rowsTotal.toLocaleString()}`
    } else {
      countText = `${rowsStr} rows`
    }
  } else if (sourceName) {
    countText = `Loading ${sourceName}…`
  } else {
    countText = "Preparing…"
  }

  const stepText = step != null && totalSteps != null ? `Step ${step} of ${totalSteps}` : null

  return (
    <div
      className="bg-amber-500 shadow-lg px-5 py-4 flex items-center gap-4"
      data-testid="materialization-progress-banner"
      role="status"
      aria-live="polite"
    >
      {/* Spinner */}
      <Loader2
        className="h-6 w-6 shrink-0 animate-spin text-amber-900"
        aria-hidden="true"
      />

      {/* Progress text block */}
      <div className="flex-1 min-w-0">
        {/* Source label + step info */}
        <div className="flex items-baseline gap-2 flex-wrap">
          <span
            className="font-semibold text-base text-amber-950"
            data-testid="materialization-banner-source"
          >
            {sourceName ? `Fetching ${sourceName}` : "Materializing data"}
          </span>
          {stepText && (
            <span className="text-sm text-amber-800 font-medium opacity-80">
              · {stepText}
            </span>
          )}
        </div>

        {/* Percentage (dominant) + count */}
        <div className="flex items-baseline gap-3 mt-0.5 flex-wrap">
          {percent != null && (
            <span
              className="text-2xl font-extrabold text-amber-950 leading-none"
              data-testid="materialization-banner-percent"
            >
              {percent}%
            </span>
          )}
          <span
            className="text-sm font-medium text-amber-900"
            data-testid="materialization-banner-rows"
          >
            {countText}
          </span>
        </div>

        {/* Progress bar — 5px tall, always visible, fills smoothly */}
        <div
          className="mt-2 h-[5px] rounded-full bg-amber-200 overflow-hidden"
          data-testid="materialization-banner-progress-bar"
        >
          <div
            className="h-full rounded-full bg-amber-900 transition-all duration-500 ease-out"
            style={{ width: percent != null ? `${Math.min(percent, 100)}%` : "100%" }}
          />
        </div>
      </div>

      {/* Stop button — solid red fill, white text, large hit target */}
      <button
        type="button"
        onClick={handleCancel}
        disabled={cancelState === "pending"}
        className={`flex items-center gap-2 rounded-lg px-5 py-2.5 text-base font-bold transition-colors shrink-0 shadow-md ${
          cancelState === "error"
            ? "bg-red-700 text-white cursor-default"
            : cancelState === "pending"
              ? "bg-red-300 text-white cursor-not-allowed"
              : "bg-red-600 text-white hover:bg-red-700 active:bg-red-800"
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
          className={`h-4 w-4 ${cancelState === "pending" ? "animate-pulse" : ""}`}
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
