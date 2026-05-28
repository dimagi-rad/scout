import { Loader2, X } from "lucide-react"
import { useState } from "react"
import { api } from "@/api/client"
import type { ActiveJob } from "@/api/jobs"

interface Props {
  job: ActiveJob
  workspaceId: string
}

/**
 * Slim status card shown above the chat input while a materialization job is
 * active. Styled to read like a system chat message — light-blue fill, blue
 * outline, rounded like a message bubble — rather than a full-bleed banner.
 *
 * The bar is determinate (filled to percent) only when the source reports a
 * row total. Keyset-paginated sources (e.g. Connect visits) return no count,
 * so there is no denominator: the bar sweeps indeterminately and we show the
 * live row count instead of a fake fill.
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
  const isDeterminate = percent != null

  // Count line: "27,000 of 50,000 rows" when a total is known, else "27,000 rows".
  let countText: string
  if (rowsLoaded > 0) {
    const rowsStr = rowsLoaded.toLocaleString()
    countText =
      rowsTotal != null && rowsTotal > 0
        ? `${rowsStr} of ${rowsTotal.toLocaleString()} rows`
        : `${rowsStr} rows`
  } else if (sourceName) {
    countText = `Loading ${sourceName}…`
  } else {
    countText = "Preparing…"
  }

  const stepText = step != null && totalSteps != null ? `Step ${step} of ${totalSteps}` : null
  const stopLabel =
    cancelState === "pending" ? "Stopping…" : cancelState === "error" ? "Try again" : "Stop"

  return (
    <div className="px-4 pt-1 pb-2">
      <div
        className="flex items-center gap-3 rounded-lg border border-blue-600/40 bg-blue-50 px-4 py-2.5 dark:border-blue-400/30 dark:bg-blue-950/40"
        data-testid="materialization-progress-banner"
        role="status"
        aria-live="polite"
      >
        <Loader2
          className="h-4 w-4 shrink-0 animate-spin text-blue-600 dark:text-blue-400"
          aria-hidden="true"
        />

        {/* Progress text block */}
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 flex-wrap">
            <span
              className="text-sm font-medium text-blue-900 dark:text-blue-100"
              data-testid="materialization-banner-source"
            >
              {sourceName ? `Fetching ${sourceName}` : "Materializing data"}
            </span>
            {stepText && (
              <span className="text-xs text-blue-700/70 dark:text-blue-300/70">{stepText}</span>
            )}
            {percent != null && (
              <span
                className="text-xs font-semibold text-blue-700 dark:text-blue-300"
                data-testid="materialization-banner-percent"
              >
                {percent}%
              </span>
            )}
          </div>

          <div
            className="text-xs text-blue-700/90 dark:text-blue-300/90 mt-0.5"
            data-testid="materialization-banner-rows"
          >
            {countText}
          </div>

          {/* Determinate fill when a total is known; otherwise an indeterminate
              sweep — keyset-paginated sources report no total to fill against. */}
          <div
            className="relative mt-1.5 h-1 overflow-hidden rounded-full bg-blue-200/70 dark:bg-blue-900"
            data-testid="materialization-banner-progress-bar"
          >
            {isDeterminate ? (
              <div
                className="h-full rounded-full bg-blue-600 transition-all duration-500 ease-out dark:bg-blue-400"
                style={{ width: `${Math.min(percent, 100)}%` }}
              />
            ) : (
              <div
                className="absolute top-0 h-full rounded-full bg-blue-500 dark:bg-blue-400"
                style={{ animation: "mat-indeterminate 1.5s ease-in-out infinite" }}
              />
            )}
          </div>
        </div>

        {/* Stop button — restrained by default, reveals a red destructive tone on hover */}
        <button
          type="button"
          onClick={handleCancel}
          disabled={cancelState === "pending"}
          className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors shrink-0 ${
            cancelState === "pending"
              ? "border-blue-600/20 text-blue-400 cursor-not-allowed dark:border-blue-400/20 dark:text-blue-500"
              : "border-blue-600/30 text-blue-700 hover:border-red-400 hover:bg-red-50 hover:text-red-600 dark:border-blue-400/30 dark:text-blue-300 dark:hover:border-red-400/50 dark:hover:bg-red-950/40 dark:hover:text-red-400"
          }`}
          data-testid="materialization-banner-stop-btn"
          title={
            cancelState === "error" ? "Cancel failed — try again" : "Stop data loading"
          }
        >
          <X
            className={`h-3.5 w-3.5 ${cancelState === "pending" ? "animate-pulse" : ""}`}
            aria-hidden="true"
          />
          <span>{stopLabel}</span>
        </button>

        <style>{`
          @keyframes mat-indeterminate {
            0%   { left: -35%; width: 35%; }
            100% { left: 100%; width: 35%; }
          }
        `}</style>
      </div>
    </div>
  )
}
