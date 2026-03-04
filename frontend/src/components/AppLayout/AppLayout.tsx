import { useEffect, useRef } from "react"
import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { ConnectionPromptDialog } from "@/components/ConnectionPromptDialog/ConnectionPromptDialog"
import { OfflineBanner } from "@/components/OfflineBanner/OfflineBanner"
import { useNetworkStatus } from "@/hooks/useNetworkStatus"
import { useAppStore } from "@/store/store"

export function AppLayout() {
  const { isOnline } = useNetworkStatus()
  const prevIsOnlineRef = useRef(isOnline)

  const dictionaryStatus = useAppStore((s) => s.dictionaryStatus)
  const recipeStatus = useAppStore((s) => s.recipeStatus)
  const knowledgeStatus = useAppStore((s) => s.knowledgeStatus)
  const artifactsStatus = useAppStore((s) => s.artifactsStatus)

  const fetchDictionary = useAppStore((s) => s.dictionaryActions.fetchDictionary)
  const fetchRecipes = useAppStore((s) => s.recipeActions.fetchRecipes)
  const fetchKnowledge = useAppStore((s) => s.knowledgeActions.fetchKnowledge)
  const fetchArtifacts = useAppStore((s) => s.artifactActions.fetchArtifacts)

  // Auto-retry errored slices when we come back online
  useEffect(() => {
    const wasOffline = !prevIsOnlineRef.current
    prevIsOnlineRef.current = isOnline

    if (isOnline && wasOffline) {
      if (dictionaryStatus === "error") fetchDictionary()
      if (recipeStatus === "error") fetchRecipes()
      if (knowledgeStatus === "error") fetchKnowledge()
      if (artifactsStatus === "error") fetchArtifacts()
    }
  }, [
    isOnline,
    dictionaryStatus,
    recipeStatus,
    knowledgeStatus,
    artifactsStatus,
    fetchDictionary,
    fetchRecipes,
    fetchKnowledge,
    fetchArtifacts,
  ])

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 min-w-0 overflow-auto">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
      <ArtifactPanel />
      <ConnectionPromptDialog />
      <OfflineBanner />
    </div>
  )
}
