import { useEffect, useRef, useState } from "react"
import { Link, Navigate, useParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"

import {
  ArtifactActions,
  ArtifactCanvas,
  type ArtifactCanvasHandle,
  ArtifactDataDialog,
  useArtifactDetail,
  useArtifactQueryData,
} from "@/components/ArtifactViewer"
import { Button } from "@/components/ui/button"
import { useAppStore } from "@/store/store"

export function ArtifactDetailPage() {
  const { artifactId } = useParams()
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const closeArtifact = useAppStore((s) => s.uiActions.closeArtifact)

  useEffect(() => {
    closeArtifact()
  }, [closeArtifact])

  if (!artifactId) {
    return <Navigate to="/artifacts" replace />
  }

  return (
    <div className="mx-auto flex h-full w-full max-w-7xl flex-col px-4 py-6 sm:px-6 lg:px-8" data-testid="artifact-detail-page">
      {activeDomainId ? (
        <ArtifactDetailContent artifactId={artifactId} workspaceId={activeDomainId} />
      ) : (
        <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground">
          Select a workspace to view this artifact.
        </div>
      )}
    </div>
  )
}

function ArtifactDetailContent({ artifactId, workspaceId }: { artifactId: string; workspaceId: string }) {
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
    <>
      <div className="mb-6 space-y-4">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/artifacts" data-testid="artifact-back-link">
            <ArrowLeft className="h-4 w-4" />
            Artifacts
          </Link>
        </Button>

        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-4">
          <div className="min-w-0">
            <h1 className="break-words text-2xl font-semibold tracking-normal" data-testid="artifact-detail-title">
              {artifact?.title ?? (isLoading ? "Loading artifact..." : "Artifact")}
            </h1>
          </div>
          <ArtifactActions
            onViewData={handleViewData}
            onExportPdf={() => canvasRef.current?.exportPdf()}
            exportDisabled={!artifact}
          />
        </div>
      </div>

      <div className="flex min-h-[calc(100vh-12rem)] flex-col overflow-hidden">
        <ArtifactCanvas
          ref={canvasRef}
          artifactId={artifactId}
          workspaceId={workspaceId}
          artifact={artifact}
          isLoading={isLoading}
          error={error}
          onQueryData={setQueryData}
        />
      </div>

      <ArtifactDataDialog
        open={dataOpen}
        onOpenChange={setDataOpen}
        artifactTitle={artifact?.title}
        queryData={queryData}
        isLoading={isDataLoading}
        error={dataError}
        onRefresh={() => void refetchData()}
      />
    </>
  )
}
