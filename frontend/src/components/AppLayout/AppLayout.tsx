import { useEffect } from "react"
import { Outlet } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { Sidebar } from "@/components/Sidebar"

export function AppLayout() {
  const fetchProjects = useAppStore((s) => s.projectActions.fetchProjects)
  const projectsStatus = useAppStore((s) => s.projectsStatus)

  useEffect(() => {
    if (projectsStatus === "idle") {
      fetchProjects()
    }
  }, [fetchProjects, projectsStatus])

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
