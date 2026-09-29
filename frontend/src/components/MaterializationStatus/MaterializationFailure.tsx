import { AlertTriangle, RotateCw, XCircle } from "lucide-react"
import { jobsApi, type RecentTermination } from "@/api/jobs"
import { useRetryableAction } from "@/hooks/useRetryableAction"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"

const RETRY_FAILED = "Retry failed — try again"

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
 * Retry is guarded by useRetryableAction so a rapid double-click cannot fire
 * two dispatches, and a final denial disables it with the server's reason.
 * After the POST returns, the polling hook will surface the new active job and
 * the parent re-renders the progress card.
 */
export function MaterializationFailure({
  termination,
  workspaceId,
  threadId,
  onRetryDispatched,
}: Props) {
  const isCancelled = termination.state === "cancelled"
  const { canWrite } = useWorkspaceRole(workspaceId)
  const retry = useRetryableAction(RETRY_FAILED, canWrite)

  const handleRetry = async () => {
    if (retry.blocked) return
    const ok = await retry.run(() =>
      jobsApi.retryMaterialization(workspaceId, {
        thread_id: threadId,
        tool_call_id: termination.tool_call_id,
      }),
    )
    if (!ok) return
    onRetryDispatched?.()
    // Leave button disabled briefly; the next poll cycle will swap this
    // card out for the progress card.
    retry.settle(1500)
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
          {retry.failure && (
            <div
              className="text-red-600 dark:text-red-400 mt-1 whitespace-pre-wrap break-words"
              role="alert"
              data-testid="materialization-retry-error"
            >
              {retry.failure.message}
            </div>
          )}
        </div>
        {termination.retry_available && canWrite && (
          <button
            type="button"
            onClick={handleRetry}
            disabled={retry.blocked}
            className={`flex items-center gap-1 px-2 py-1 rounded text-xs transition-colors shrink-0 ${
              retry.blocked
                ? "text-muted-foreground border border-border cursor-not-allowed"
                : retry.state === "error"
                  ? "text-red-500 border border-red-500/40"
                  : "text-red-600 hover:bg-red-500/10 border border-red-500/30"
            }`}
            data-testid="materialization-retry-btn"
            title={retry.failure?.message ?? "Retry materialization"}
          >
            <RotateCw
              className={`w-3 h-3 ${retry.state === "pending" ? "animate-spin" : ""}`}
            />
            <span>
              {retry.state === "pending"
                ? "Retrying..."
                : retry.failure
                  ? retry.failure.retryable
                    ? "Retry failed"
                    : "Can't retry"
                  : "Retry"}
            </span>
          </button>
        )}
      </div>
    </div>
  )
}
