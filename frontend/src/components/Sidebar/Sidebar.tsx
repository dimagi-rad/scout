import { useEffect, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import {
  MessageSquare,
  BookOpen,
  ChefHat,
  Database,
  LayoutDashboard,
  LogOut,
  Plus,
  Link2,
  ChevronsUpDown,
} from "lucide-react"
import { useAppStore } from "@/store/store"
import { NavItem } from "./NavItem"
import { TenantManagement } from "./TenantManagement"
import { Button } from "@/components/ui/button"
import { WorkspaceSelector } from "@/components/WorkspaceSelector/WorkspaceSelector"

export function Sidebar() {
  const navigate = useNavigate()
  const [selectorOpen, setSelectorOpen] = useState(false)
  const user = useAppStore((s) => s.user)
  const domains = useAppStore((s) => s.domains)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const logout = useAppStore((s) => s.authActions.logout)
  const threadId = useAppStore((s) => s.threadId)
  const threads = useAppStore((s) => s.threads)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)
  const workspaceMode = useAppStore((s) => s.workspaceMode)
  const activeCustomWorkspace = useAppStore((s) => s.activeCustomWorkspace)

  // Compute display label
  const workspaceLabel = (() => {
    if (workspaceMode === "custom" && activeCustomWorkspace) {
      return activeCustomWorkspace.name
    }
    if (activeDomainId) {
      const domain = domains.find((d) => d.id === activeDomainId)
      return domain?.tenant_name ?? "Select Workspace"
    }
    return "Select Workspace"
  })()

  // Fetch domains on mount
  useEffect(() => {
    fetchDomains()
  }, [fetchDomains])

  // Fetch threads when domain changes
  useEffect(() => {
    if (activeDomainId) {
      fetchThreads(activeDomainId)
    }
  }, [activeDomainId, fetchThreads])

  return (
    <aside className="flex h-screen w-64 flex-col border-r bg-background">
      {/* Logo */}
      <div className="flex h-14 items-center border-b px-4">
        <Link to="/" className="flex items-center gap-2 font-semibold">
          <span className="text-lg">Scout</span>
        </Link>
      </div>

      {/* Workspace Selector */}
      <div className="border-b p-4">
        <label className="text-xs font-medium text-muted-foreground">
          Workspace
        </label>
        <button
          onClick={() => setSelectorOpen(true)}
          className="mt-1 flex w-full items-center justify-between rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm transition-colors hover:bg-accent"
          data-testid="domain-selector"
        >
          <span className="truncate">{workspaceLabel}</span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </button>
        <WorkspaceSelector open={selectorOpen} onClose={() => setSelectorOpen(false)} />
      </div>

      {/* Tenant Management (custom workspace only) */}
      {workspaceMode === "custom" && activeCustomWorkspace && (
        <TenantManagement
          workspace={activeCustomWorkspace}
          domains={domains}
          isOwner={
            activeCustomWorkspace.members?.some(
              (m) => m.email === user?.email && m.role === "owner",
            ) ?? false
          }
        />
      )}

      {/* Navigation */}
      <nav className="space-y-1 p-4">
        <NavItem to="/" icon={MessageSquare} label="Chat" />
        <NavItem to="/artifacts" icon={LayoutDashboard} label="Artifacts" />
        <NavItem to="/knowledge" icon={BookOpen} label="Knowledge" />
        <NavItem to="/recipes" icon={ChefHat} label="Recipes" />
        <NavItem to="/data-dictionary" icon={Database} label="Data Dictionary" />
        <NavItem to="/datasources" icon={Database} label="Connections" />
      </nav>

      {/* Thread History */}
      <div className="flex flex-1 flex-col border-t overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2">
          <span className="text-xs font-medium text-muted-foreground">
            Chat History
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={() => { newThread(); navigate("/chat") }}
            data-testid="sidebar-new-chat"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {threads.map((thread) => (
            <button
              key={thread.id}
              onClick={() => { selectThread(thread.id); navigate("/chat") }}
              data-testid={`sidebar-thread-${thread.id}`}
              className={`w-full rounded-md px-3 py-1.5 text-left text-sm truncate transition-colors ${
                thread.id === threadId
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              }`}
            >
              {thread.title}
            </button>
          ))}
        </div>
      </div>

      {/* User Section */}
      <div className="border-t p-4">
        <div className="mb-2 truncate text-sm text-muted-foreground">
          {user?.email}
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start"
          asChild
          data-testid="sidebar-connections"
        >
          <Link to="/settings/connections">
            <Link2 className="mr-2 h-4 w-4" />
            Connected Accounts
          </Link>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start"
          onClick={logout}
          data-testid="logout-btn"
        >
          <LogOut className="mr-2 h-4 w-4" />
          Logout
        </Button>
      </div>
    </aside>
  )
}
