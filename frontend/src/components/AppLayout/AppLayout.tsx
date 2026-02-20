import { Outlet } from "react-router-dom"
import { Sidebar } from "@/components/Sidebar"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ArtifactPanel } from "@/components/ArtifactPanel/ArtifactPanel"

export function AppLayout() {
  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 min-w-0 overflow-auto">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
      <ArtifactPanel />
    </div>
  )
}
