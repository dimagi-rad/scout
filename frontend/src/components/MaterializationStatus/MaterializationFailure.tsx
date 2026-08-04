import { useState } from "react"
import { AlertTriangle, Link2, RotateCw, XCircle } from "lucide-react"
import { AUTH_TOKEN_EXPIRED, jobsApi, type RecentTermination } from "@/api/jobs"
import { withBasePath } from "@/config"

interface Props {
  termination: RecentTermination
  workspaceId: string
  threadId: string
  onRetryDispatched?: () => void
}

/**
 * Inline failure card rendered on a run_materialization tool-call once the
 * spinner clears and the ThreadJob ended in FAILED or CANCELLED. Surfaces the
 * server-composed error_summary and a Retry button.
 *
 * Retry is guarded by local state (idle | pending) so a rapid double-click
 * cannot fire two dispatches. After the POST returns, the polling hook will
 * surface the new active job and the parent re-renders the progress card.
 */
export function MaterializationFailure({
  termination,
  workspaceId,
  threadId,
  onRetryDispatched,
}: Props) {
  const [retryState, setRetryState] = useState<"idle" | "pending" | "error">("idle")
  const isCancelled = termination.state === "cancelled"

  // Reconnect is offered ONLY for a dead credential. For AUTH_ACCESS_DENIED the
  // credential is valid, so reconnecting mints an identically-scoped token and
  // fails the same way — offering the button there is the loop #372 is about.
  // Keyed on the code, never on error_summary prose.
  const needsReconnect = (termination.credential_failures ?? []).some(
    (f) => f.code === AUTH_TOKEN_EXPIRED,
  )

  const handleRetry = async () => {
    if (retryState === "pending") return
    setRetryState("pending")
    try {
      await jobsApi.retryMaterialization(workspaceId, {
        thread_id: threadId,
        tool_call_id: termination.tool_call_id,
      })
      onRetryDispatched?.()
      // Leave button disabled briefly; the next poll cycle will swap this
      // card out for the progress card.
      setTimeout(() => setRetryState("idle"), 1500)
    } catch {
      setRetryState("error")
      setTimeout(() => setRetryState("idle"), 3000)
    }
  }

  const Icon = isCancelled ? XCircle : AlertTriangle
  const headerText = isCancelled
    ? "Materialization cancelled"
    : "Materialization failed"

  return (
    <div
      className="rounded border border-red-500/30 bg-red-500/5 my-1 text-xs"
      data-testid="materialization-failure-card"
    >
      <div className="flex items-start gap-2 px-3 py-2">
        <Icon className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div
            className="font-medium text-red-600 dark:text-red-400"
            data-testid="materialization-failure-header"
          >
            {headerText}
          </div>
          {termination.error_summary && (
            <div
              className="text-muted-foreground mt-1 whitespace-pre-wrap break-words"
              data-testid="materialization-failure-summary"
            >
              {termination.error_summary}
            </div>
          )}
          {needsReconnect && (
            <a
              href={withBasePath("/settings/connections")}
              className="mt-2 inline-flex items-center gap-1 rounded border border-amber-500/40 px-2 py-1 text-amber-600 transition-colors hover:bg-amber-500/10 dark:text-amber-400"
              data-testid="materialization-reconnect-link"
            >
              <Link2 className="h-3 w-3" />
              <span>Reconnect account</span>
            </a>
          )}
        </div>
        {termination.retry_available && (
          <button
            type="button"
            onClick={handleRetry}
            disabled={retryState === "pending"}
            className={`flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors shrink-0 ${
              retryState === "error"
                ? "text-red-500 border border-red-500/40"
                : retryState === "pending"
                  ? "text-muted-foreground border border-border"
                  : "text-red-600 hover:bg-red-500/10 border border-red-500/30"
            }`}
            data-testid="materialization-retry-btn"
            title={
              retryState === "error"
                ? "Retry failed — try again"
                : "Retry materialization"
            }
          >
            <RotateCw
              className={`w-3 h-3 ${retryState === "pending" ? "animate-spin" : ""}`}
            />
            <span>
              {retryState === "pending"
                ? "Retrying..."
                : retryState === "error"
                  ? "Retry failed"
                  : "Retry"}
            </span>
          </button>
        )}
      </div>
    </div>
  )
}
