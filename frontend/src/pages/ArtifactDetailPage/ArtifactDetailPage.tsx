import { useEffect, useRef, useState } from "react"
import { Link, Navigate, useLocation, useNavigate, useParams } from "react-router-dom"
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
import { artifactPath } from "@/lib/artifactPath"

export function ArtifactDetailPage() {
  const { artifactId, workspaceId: urlWorkspaceId } = useParams()
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const workspaceId = urlWorkspaceId ?? activeDomainId
  const closeArtifact = useAppStore((s) => s.uiActions.closeArtifact)

  useEffect(() => {
    closeArtifact()
  }, [closeArtifact])

  if (!artifactId) {
    return <Navigate to="/artifacts" replace />
  }

  return (
    <div className="mx-auto flex h-full w-full max-w-7xl flex-col px-4 py-6 sm:px-6 lg:px-8" data-testid="artifact-detail-page">
      {workspaceId ? (
        <ArtifactDetailContent key={`${workspaceId}/${artifactId}`} artifactId={artifactId} workspaceId={workspaceId} />
      ) : (
        <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground">
          Select a workspace to view this artifact.
        </div>
      )}
    </div>
  )
}

function ArtifactDetailContent({ artifactId, workspaceId }: { artifactId: string; workspaceId: string }) {
  const navigate = useNavigate()
  const location = useLocation()
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const domains = useAppStore((s) => s.domains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const adoptedWorkspaceRef = useRef(false)
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

  useEffect(() => {
    if (adoptedWorkspaceRef.current) {
      if (activeDomainId && activeDomainId !== workspaceId) navigate("/artifacts")
      return
    }
    // The scoped API must authorize the link before it changes workspace context.
    if (!artifact || isLoading || error) return
    adoptedWorkspaceRef.current = true
    setActiveDomain(workspaceId)
  }, [activeDomainId, artifact, error, isLoading, navigate, setActiveDomain, workspaceId])

  useEffect(() => {
    if (!artifact || isLoading || error) return
    const workspace = domains.find((item) => item.id === workspaceId) ?? { id: workspaceId }
    const canonical = artifactPath(workspace, artifactId)
    // Qualify legacy links only after resolving them in the selected workspace.
    if (location.pathname !== canonical) navigate(canonical, { replace: true })
  }, [artifact, artifactId, domains, error, isLoading, location.pathname, navigate, workspaceId])

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
