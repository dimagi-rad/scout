import { useEffect, useRef } from "react"
import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { OfflineBanner } from "@/components/OfflineBanner/OfflineBanner"
import { LostAccessModal } from "@/components/LostAccessModal/LostAccessModal"
import { WorkspaceAddedNotice } from "@/components/WorkspaceAddedNotice/WorkspaceAddedNotice"
import { OcsAccessNotice } from "@/components/OcsAccessNotice/OcsAccessNotice"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
import { useAppStore } from "@/store/store"
import { WorkspaceJobsProvider } from "@/contexts/WorkspaceJobsContext"
import { TopBarProvider } from "@/components/TopBar"

export function AppLayout() {
  const { isOnline } = useNetworkStatus()
  const prevIsOnlineRef = useRef(isOnline)

  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const datasetStatus = useAppStore((s) => s.datasetStatus)
  const recipeStatus = useAppStore((s) => s.recipeStatus)
  const knowledgeStatus = useAppStore((s) => s.knowledgeStatus)
  const artifactsStatus = useAppStore((s) => s.artifactsStatus)

  const fetchDatasets = useAppStore((s) => s.datasetActions.fetchDatasets)
  const fetchRecipes = useAppStore((s) => s.recipeActions.fetchRecipes)
  const fetchKnowledge = useAppStore((s) => s.knowledgeActions.fetchKnowledge)
  const fetchArtifacts = useAppStore((s) => s.artifactActions.fetchArtifacts)

  // Auto-retry errored slices when we come back online
  useEffect(() => {
    const wasOffline = !prevIsOnlineRef.current
    prevIsOnlineRef.current = isOnline

    if (isOnline && wasOffline) {
      if (datasetStatus === "error") fetchDatasets()
      if (recipeStatus === "error") fetchRecipes()
      if (knowledgeStatus === "error") fetchKnowledge()
      if (artifactsStatus === "error") fetchArtifacts()
    }
  }, [
    isOnline,
    datasetStatus,
    recipeStatus,
    knowledgeStatus,
    artifactsStatus,
    fetchDatasets,
    fetchRecipes,
    fetchKnowledge,
    fetchArtifacts,
  ])

  return (
    <WorkspaceJobsProvider workspaceId={activeDomainId}>
      <div className="flex h-screen">
        <Sidebar />
        <div className="flex flex-1 min-w-0 flex-col">
          <TopBarProvider>
            <main className="flex-1 min-w-0 overflow-auto">
              <ErrorBoundary>
                <Outlet />
              </ErrorBoundary>
            </main>
          </TopBarProvider>
        </div>
        <ArtifactPanel />
        <OfflineBanner />
        <LostAccessModal />
        <WorkspaceAddedNotice />
        {/* Below the h-11 TopBar so its page actions stay clickable. */}
        <div className="fixed right-4 top-14 z-40 w-96 max-w-[calc(100vw-2rem)]">
          <OcsAccessNotice showConnectionsLink />
        </div>
      </div>
    </WorkspaceJobsProvider>
  )
}
