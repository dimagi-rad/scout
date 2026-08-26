import { useRef, useState } from "react"

import { cn } from "@/lib/utils"
import { ArtifactActions } from "./ArtifactActions"
import { ArtifactCanvas, type ArtifactCanvasHandle } from "./ArtifactCanvas"
import { ArtifactDataDialog } from "./ArtifactDataDialog"
import { useArtifactDetail } from "./useArtifactDetail"
import { useArtifactQueryData } from "./useArtifactQueryData"

interface ArtifactViewerProps {
  artifactId: string
  workspaceId: string
  className?: string
  onClose?: () => void
}

export function ArtifactViewer({ artifactId, workspaceId, className, onClose }: ArtifactViewerProps) {
  const [dataOpen, setDataOpen] = useState(false)
  const canvasRef = useRef<ArtifactCanvasHandle>(null)
  const { artifact, isLoading, error } = useArtifactDetail(artifactId, workspaceId)
  const {
    queryData,
    isLoading: isDataLoading,
    error: dataError,
    refetch: refetchData,
    setQueryData,
  } = useArtifactQueryData(artifactId, workspaceId)

  function handleViewData() {
    setDataOpen(true)
    if (!queryData && !isDataLoading) {
      void refetchData()
    }
  }

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-background", className)}>
      <div className="flex min-h-14 shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">
            {artifact?.title ?? "Artifact"}
          </div>
        </div>
        <ArtifactActions
          onViewData={handleViewData}
          onExportPdf={() => canvasRef.current?.exportPdf()}
          onClose={onClose}
          exportDisabled={!artifact}
        />
      </div>

      <ArtifactCanvas
        ref={canvasRef}
        artifactId={artifactId}
        workspaceId={workspaceId}
        artifact={artifact}
        isLoading={isLoading}
        error={error}
        onQueryData={setQueryData}
      />
      <ArtifactDataDialog
        open={dataOpen}
        onOpenChange={setDataOpen}
        artifactTitle={artifact?.title}
        queryData={queryData}
        isLoading={isDataLoading}
        error={dataError}
        onRefresh={() => void refetchData()}
      />
    </div>
  )
}
