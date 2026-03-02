import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { useEmbedParams } from "@/hooks/useEmbedParams"

export function EmbedLayout() {
  const { mode } = useEmbedParams()
  const showSidebar = mode === "full"
  const showArtifacts = mode === "full" || mode === "chat+artifacts"

  return (
    <div className="flex h-screen">
      {showSidebar && <Sidebar />}
      <main className="flex-1 min-w-0 overflow-auto">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
      {showArtifacts && <ArtifactPanel />}
    </div>
  )
}
