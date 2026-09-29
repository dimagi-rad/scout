import { forwardRef, useEffect, useImperativeHandle, useRef } from "react"
import { Loader2 } from "lucide-react"

import { ArtifactGraphRenderer, type ArtifactDetail } from "@/components/ArtifactGraph"
import type { DateRange } from "@/components/ArtifactGraph/types"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { withBasePath } from "@/config"
import { useWorkspaceRole } from "@/hooks/useWorkspaceRole"
import { reportRenderError, SAFE_ERROR_NAME } from "@/lib/reportRenderError"
import { cn } from "@/lib/utils"
import { ArtifactDataRecovery } from "./ArtifactDataRecovery"
import { useArtifactDataRecovery } from "./useArtifactDataRecovery"
import { useArtifactPrint } from "./useArtifactPrint"

function sandboxErrorName(name: unknown, stack: unknown): string {
  if (typeof name === "string" && SAFE_ERROR_NAME.test(name)) return name
  if (typeof stack === "string") {
    const fromStack = /^([A-Z][\w$]{0,60}):/m.exec(stack)?.[1]
    if (fromStack) return fromStack
  }
  return "SandboxRenderError"
}

export interface ArtifactCanvasHandle {
  exportPdf: () => void
}

interface ArtifactCanvasProps {
  artifactId: string
  workspaceId: string
  artifact: ArtifactDetail | null
  isLoading: boolean
  error: string | null
  className?: string
  onDateSourcesChange?: (sources: Record<string, DateRange | null>) => void
}

export const ArtifactCanvas = forwardRef<ArtifactCanvasHandle, ArtifactCanvasProps>(
  function ArtifactCanvas(
    { artifactId, workspaceId, artifact, isLoading, error, className, onDateSourcesChange },
    ref,
  ) {
    const iframeRef = useRef<HTMLIFrameElement>(null)
    const { printRef, printArtifact, printError } = useArtifactPrint(artifactId)
    const isGraphArtifact = artifact?.type === "story"
    const hasLiveQueries = Boolean(artifact?.semantic_queries.length)
    const recovery = useArtifactDataRecovery(artifactId, workspaceId, hasLiveQueries)
    const { canWrite } = useWorkspaceRole(workspaceId)
    const dataIsReady =
      !hasLiveQueries ||
      (recovery.state?.queryable ?? (
        recovery.state?.status === "ready" ||
        recovery.state?.status === "not_required"
      ))
    const showRecovery = !dataIsReady || Boolean(
      recovery.error || recovery.state?.recovery_action || recovery.state?.status === "recovering",
    )
    const dataKey = `${artifactId}:${recovery.state?.data_revision ?? "initial"}`

    useImperativeHandle(ref, () => ({
      exportPdf: () => {
        if (isGraphArtifact) {
          printArtifact()
          return
        }
        // The sandboxed iframe has an opaque origin, so a concrete targetOrigin
        // would be dropped. The message has no sensitive payload and is sent
        // only to this iframe's contentWindow.
        iframeRef.current?.contentWindow?.postMessage({ type: "scout-print" }, "*")
      },
    }), [isGraphArtifact, printArtifact])

    const artifactVersion = artifact?.version

    useEffect(() => {
      function handleMessage(event: MessageEvent) {
        if (event.source !== iframeRef.current?.contentWindow) return
        if (event.data?.type === "artifact-error") {
          // Only the error text leaves the page; nothing else in the message is read.
          const { title, message, details, name } = event.data.error ?? {}
          reportRenderError({
            source: "sandbox",
            stage: typeof title === "string" ? title : undefined,
            name: sandboxErrorName(name, details),
            message: typeof message === "string" ? message : "",
            stack: typeof details === "string" ? details : undefined,
            artifactId,
            artifactVersion,
          })
        }
      }
      window.addEventListener("message", handleMessage)
      return () => window.removeEventListener("message", handleMessage)
    }, [artifactId, artifactVersion])

    return (
      <div className={cn("flex min-h-0 flex-1 flex-col bg-background", className)}>
        {printError && (
          <p role="alert" className="px-4 py-3 text-sm text-destructive">{printError}</p>
        )}
        {(isLoading || !artifact) && !error && (
          <div className="flex flex-1 items-center justify-center text-muted-foreground">
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            <span className="text-sm">Loading artifact...</span>
          </div>
        )}
        {error && (
          <div className="flex flex-1 items-center justify-center p-4 text-sm text-destructive">
            {error}
          </div>
        )}
        {!isLoading && !error && artifact && (
          <div
            ref={dataIsReady && isGraphArtifact ? printRef : undefined}
            className="flex min-h-0 flex-1 flex-col"
          >
            {hasLiveQueries && showRecovery && (
              <ArtifactDataRecovery
                readable={dataIsReady}
                state={recovery.state}
                error={recovery.error}
                isChecking={recovery.isChecking}
                isStarting={recovery.isStarting}
                onRecover={() => void recovery.startRecovery()}
                onRetryCheck={() => void recovery.refetch()}
                canRecover={canWrite}
              />
            )}
            {dataIsReady && isGraphArtifact && (
              <ErrorBoundary resetKey={dataKey} artifactId={artifactId} artifactVersion={artifactVersion}>
                <ArtifactGraphRenderer artifact={artifact} workspaceId={workspaceId} dataRevision={recovery.state?.data_revision} onDateSourcesChange={onDateSourcesChange} />
              </ErrorBoundary>
            )}
            {dataIsReady && !isGraphArtifact && (
              <iframe
                ref={iframeRef}
                key={dataKey}
                src={withBasePath(`/api/workspaces/${workspaceId}/artifacts/${artifactId}/sandbox/`)}
                className="flex-1 w-full"
                // SECURITY: deliberately NO allow-same-origin. The sandbox doc is
                // served same-origin and session-authenticated, and it executes
                // agent-generated code. With allow-same-origin, that code could read
                // cookies/CSRF token, issue credentialed /api/ requests, and reach
                // window.parent. Omitting it gives the frame an opaque origin.
                sandbox="allow-scripts allow-modals"
                title={artifact.title || "Artifact"}
                data-testid={`artifact-frame-${artifactId}`}
              />
            )}
          </div>
        )}
      </div>
    )
  },
)
