import { Link } from "react-router-dom"
import {
  MessageSquare,
  BookOpen,
  ChefHat,
  Database,
  Cloud,
  Settings,
  LogOut,
} from "lucide-react"
import { useAppStore } from "@/store/store"
import { NavItem } from "./NavItem"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export function Sidebar() {
  const user = useAppStore((s) => s.user)
  const projects = useAppStore((s) => s.projects)
  const activeProjectId = useAppStore((s) => s.activeProjectId)
  const setActiveProject = useAppStore((s) => s.projectActions.setActiveProject)
  const logout = useAppStore((s) => s.authActions.logout)

  const activeProject = projects.find((p) => p.id === activeProjectId)

  return (
    <aside className="flex h-screen w-64 flex-col border-r bg-background">
      {/* Logo */}
      <div className="flex h-14 items-center border-b px-4">
        <Link to="/" className="flex items-center gap-2 font-semibold">
          <span className="text-lg">Scout</span>
        </Link>
      </div>

      {/* Project Selector */}
      <div className="border-b p-4">
        <label className="text-xs font-medium text-muted-foreground">
          Project
        </label>
        <Select
          value={activeProjectId ?? undefined}
          onValueChange={setActiveProject}
        >
          <SelectTrigger className="mt-1 w-full">
            <SelectValue placeholder="Select project">
              {activeProject?.name}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {projects.map((project) => (
              <SelectItem key={project.id} value={project.id}>
                {project.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Link
          to="/projects"
          className="mt-2 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <Settings className="h-3 w-3" />
          Manage Projects
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 p-4">
        <NavItem to="/" icon={MessageSquare} label="Chat" />
        <NavItem to="/knowledge" icon={BookOpen} label="Knowledge" />
        <NavItem to="/recipes" icon={ChefHat} label="Recipes" />
        <NavItem to="/data-dictionary" icon={Database} label="Data Dictionary" />
        <NavItem to="/datasources" icon={Cloud} label="Data Sources" />
      </nav>

      {/* User Section */}
      <div className="border-t p-4">
        <div className="mb-2 truncate text-sm text-muted-foreground">
          {user?.email}
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start"
          onClick={logout}
        >
          <LogOut className="mr-2 h-4 w-4" />
          Logout
        </Button>
      </div>
    </aside>
  )
}
