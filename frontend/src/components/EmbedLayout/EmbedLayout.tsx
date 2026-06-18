import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"
import { useAutoResize } from "@/hooks/useAutoResize"
import { useAppStore } from "@/store/store"
import { WorkspaceJobsProvider } from "@/contexts/WorkspaceJobsContext"
import { useEmbedSettings } from "@/contexts/EmbedSettingsContext"

export function EmbedLayout() {
  // Live mode from the embed settings context so the widget's runtime
  // scout:set-mode command takes effect (issue #248, 06#6), rather than the
  // value frozen at first URL read.
  const { mode } = useEmbedSettings()
  const showSidebar = mode === "full"
  const showArtifacts = mode === "full" || mode === "chat+artifacts"
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  useAutoResize()

  return (
    <WorkspaceJobsProvider workspaceId={activeDomainId}>
      <div className="flex h-screen min-h-[600px]">
        {showSidebar && <Sidebar />}
        <main className="flex-1 min-w-0 overflow-auto">
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </main>
        {showArtifacts && <ArtifactPanel />}
      </div>
    </WorkspaceJobsProvider>
  )
}
