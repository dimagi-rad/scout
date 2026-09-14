import { AlertTriangle, DatabaseBackup, Loader2, RefreshCw } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import type { ArtifactDataRecoveryState } from "./types"

interface ArtifactDataRecoveryProps {
  state: ArtifactDataRecoveryState | null
  error: string | null
  isChecking: boolean
  isStarting: boolean
  onRecover: () => void
  onRetryCheck: () => void
}

export function ArtifactDataRecovery({
  state,
  error,
  isChecking,
  isStarting,
  onRecover,
  onRetryCheck,
}: ArtifactDataRecoveryProps) {
  if (isChecking) {
    return (
      <div
        className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground"
        data-testid="artifact-data-checking"
        role="status"
      >
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        Checking data availability…
      </div>
    )
  }

  if (error) {
    return (
      <RecoveryShell icon={<AlertTriangle className="h-5 w-5" />} title="Data check failed">
        <p>{error}</p>
        <Button variant="outline" size="sm" onClick={onRetryCheck} data-testid="artifact-data-check-retry">
          <RefreshCw />
          Try again
        </Button>
      </RecoveryShell>
    )
  }

  if (!state) return null

  if (state.status === "recovering") {
    const progress = state.recovery?.progress
    const source = progress?.source
    const percent = progress?.percent
    const rowsLoaded = progress?.rows_loaded ?? 0
    const unit = progress?.unit ?? "rows"
    return (
      <RecoveryShell
        icon={<Loader2 className="h-5 w-5 animate-spin" />}
        title={state.recovery?.type === "semantic_rebuild" ? "Rebuilding the data model" : "Restoring artifact data"}
      >
        <p>{state.message}</p>
        <div className="w-full max-w-sm" role="status" aria-live="polite">
          <div className="mb-1.5 flex items-center justify-between gap-3 text-xs text-muted-foreground">
            <span>{source ? `Loading ${source}` : progress?.message || "Preparing workspace data"}</span>
            {percent != null && <span className="font-medium text-foreground">{percent}%</span>}
          </div>
          <div className="relative h-1.5 overflow-hidden rounded-full bg-muted">
            {percent != null ? (
              <div
                className="h-full rounded-full bg-primary transition-all duration-500"
                style={{ width: `${percent}%` }}
              />
            ) : (
              <div className="artifact-recovery-sweep absolute h-full w-1/3 rounded-full bg-primary" />
            )}
          </div>
          {rowsLoaded > 0 && (
            <p className="mt-1.5 text-xs text-muted-foreground">
              {rowsLoaded.toLocaleString()} {unit} loaded
            </p>
          )}
        </div>
        <style>{`
          @keyframes artifact-recovery-sweep {
            from { left: -33%; }
            to { left: 100%; }
          }
          .artifact-recovery-sweep { animation: artifact-recovery-sweep 1.5s ease-in-out infinite; }
        `}</style>
      </RecoveryShell>
    )
  }

  const failed = state.status === "failed"
  const semanticOnly = state.recovery_action === "semantic_rebuild"
  return (
    <RecoveryShell
      icon={failed ? <AlertTriangle className="h-5 w-5" /> : <DatabaseBackup className="h-5 w-5" />}
      title={failed ? "Data recovery failed" : semanticOnly ? "Data model needs rebuilding" : "Artifact data is offline"}
      destructive={failed}
    >
      <p>{state.message}</p>
      {state.detail && failed && <p className="text-xs text-muted-foreground">{state.detail}</p>}
      {state.recovery_action && (
        <Button onClick={onRecover} disabled={isStarting} data-testid="artifact-data-recover">
          {isStarting ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          {isStarting
            ? "Starting…"
            : failed
              ? "Try recovery again"
              : semanticOnly
                ? "Rebuild data model"
                : "Restore workspace data"}
        </Button>
      )}
    </RecoveryShell>
  )
}

function RecoveryShell({
  icon,
  title,
  destructive = false,
  children,
}: {
  icon: ReactNode
  title: string
  destructive?: boolean
  children: ReactNode
}) {
  return (
    <div className="flex flex-1 items-center justify-center p-6" data-testid="artifact-data-recovery">
      <div className="w-full max-w-lg rounded-xl border bg-card px-6 py-7 text-center shadow-sm">
        <div
          className={`mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-full ${
            destructive ? "bg-destructive/10 text-destructive" : "bg-primary/10 text-primary"
          }`}
        >
          {icon}
        </div>
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
        <div className="mx-auto mt-2 flex max-w-md flex-col items-center gap-4 text-sm text-muted-foreground">
          {children}
        </div>
      </div>
    </div>
  )
}
