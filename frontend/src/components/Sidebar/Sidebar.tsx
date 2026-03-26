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
  ChevronDown,
} from "lucide-react"
import { useAppStore } from "@/store/store"
import { useEmbedParams } from "@/hooks/useEmbedParams"
import { NavItem } from "./NavItem"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"

export function Sidebar() {
  const navigate = useNavigate()
  const { isEmbed } = useEmbedParams()
  const prefix = isEmbed ? "/embed" : ""
  const user = useAppStore((s) => s.user)
  const domains = useAppStore((s) => s.domains)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const logout = useAppStore((s) => s.authActions.logout)
  const threadId = useAppStore((s) => s.threadId)
  const threads = useAppStore((s) => s.threads)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)
  const [showCreateModal, setShowCreateModal] = useState(false)

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
        <Link to={`${prefix}/`} className="flex items-center gap-2 font-semibold">
          <span className="text-lg">Scout</span>
        </Link>
      </div>

      {/* Workspace Selector */}
      <div className="border-b p-4">
        <label className="text-xs font-medium text-muted-foreground">Workspace</label>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              className="mt-1 w-full justify-between font-normal"
              data-testid="domain-selector"
            >
              <span className="truncate">
                {domains.find((d) => d.id === activeDomainId)?.name ?? "Select workspace"}
              </span>
              <ChevronDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-56">
            {domains.map((d) => (
              <DropdownMenuItem
                key={d.id}
                data-testid={`domain-item-${d.id}`}
                onSelect={() => { setActiveDomain(d.id); newThread() }}
                className={d.id === activeDomainId ? "font-medium" : ""}
              >
                {d.name}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => navigate(`${prefix}/workspaces`)}>
              Manage workspaces…
            </DropdownMenuItem>
            {/* Defer modal open so Radix can finish closing the dropdown before the Dialog mounts its own focus trap */}
            <DropdownMenuItem onSelect={() => setTimeout(() => setShowCreateModal(true), 0)}>
              + New workspace
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {showCreateModal && (
          <CreateWorkspaceModal onClose={() => setShowCreateModal(false)} />
        )}
      </div>

      {/* Navigation */}
      <nav className="space-y-1 p-4">
        <NavItem to={`${prefix}/`} icon={MessageSquare} label="Chat" />
        <NavItem to={`${prefix}/artifacts`} icon={LayoutDashboard} label="Artifacts" />
        <NavItem to={`${prefix}/knowledge`} icon={BookOpen} label="Knowledge" />
        <NavItem to={`${prefix}/recipes`} icon={ChefHat} label="Recipes" />
        <NavItem to={`${prefix}/data-dictionary`} icon={Database} label="Data Dictionary" />
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
            onClick={() => { newThread(); navigate(`${prefix}/chat`) }}
            data-testid="sidebar-new-chat"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {threads.map((thread) => (
            <button
              key={thread.id}
              onClick={() => { selectThread(thread.id); navigate(`${prefix}/chat`) }}
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
          <Link to={`${prefix}/settings/connections`}>
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
