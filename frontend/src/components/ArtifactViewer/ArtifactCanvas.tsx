import { forwardRef, useEffect, useImperativeHandle, useRef } from "react"
import { Loader2 } from "lucide-react"

import { ArtifactGraphRenderer, type ArtifactDetail } from "@/components/ArtifactGraph"
import { withBasePath } from "@/config"
import { cn } from "@/lib/utils"
import { ArtifactDataRecovery } from "./ArtifactDataRecovery"
import type { QueryDataResponse } from "./types"
import { useArtifactDataRecovery } from "./useArtifactDataRecovery"
import { useArtifactPrint } from "./useArtifactPrint"

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
  onQueryData?: (queryData: QueryDataResponse) => void
}

export const ArtifactCanvas = forwardRef<ArtifactCanvasHandle, ArtifactCanvasProps>(
  function ArtifactCanvas(
    { artifactId, workspaceId, artifact, isLoading, error, className, onQueryData },
    ref,
  ) {
    const iframeRef = useRef<HTMLIFrameElement>(null)
    const { printRef, printArtifact, printError } = useArtifactPrint(artifactId)
    const isGraphArtifact = artifact?.type === "story"
    const hasLiveQueries = Boolean(artifact?.semantic_queries.length)
    const recovery = useArtifactDataRecovery(artifactId, workspaceId, hasLiveQueries)
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

    useEffect(() => {
      function handleMessage(event: MessageEvent) {
        if (!onQueryData) return
        if (event.source !== iframeRef.current?.contentWindow) return
        if (event.data?.type === "artifact-query-data" && event.data.artifactId === artifactId) {
          onQueryData(event.data.queryData as QueryDataResponse)
        }
      }
      window.addEventListener("message", handleMessage)
      return () => window.removeEventListener("message", handleMessage)
    }, [artifactId, onQueryData])

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
              />
            )}
            {dataIsReady && isGraphArtifact && (
              <ArtifactGraphRenderer artifact={artifact} workspaceId={workspaceId} dataRevision={recovery.state?.data_revision} />
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
