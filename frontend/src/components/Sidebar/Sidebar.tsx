import { useEffect, useState } from "react"
import { Link, useLocation, useNavigate } from "react-router-dom"
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
  Search,
  Settings,
} from "lucide-react"
import { useAppStore } from "@/store/store"
import { NavItem } from "./NavItem"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"

export function Sidebar() {
  const navigate = useNavigate()
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
  const [selectorOpen, setSelectorOpen] = useState(false)
  const [wsSearch, setWsSearch] = useState("")
  const location = useLocation()
  const pathPrefix = location.pathname.startsWith("/embed") ? "/embed" : ""

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
        <Link to={`${pathPrefix}/`} className="flex items-center gap-2 font-semibold">
          <span className="text-lg">Scout</span>
        </Link>
      </div>

      {/* Workspace Selector */}
      <div className="border-b p-4">
        <label className="text-xs font-medium text-muted-foreground">Workspace</label>
        <Popover open={selectorOpen} onOpenChange={(open) => { setSelectorOpen(open); if (!open) setWsSearch("") }}>
          <PopoverTrigger asChild>
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
          </PopoverTrigger>
          <PopoverContent className="w-56 p-0" align="start">
            {/* Search input */}
            <div className="border-b p-2">
              <div className="relative">
                <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  placeholder="Search workspaces..."
                  value={wsSearch}
                  onChange={(e) => setWsSearch(e.target.value)}
                  className="h-8 pl-7 text-sm"
                  data-testid="workspace-search"
                />
              </div>
            </div>
            {/* Scrollable workspace list */}
            <div className="max-h-60 overflow-y-auto p-1">
              {domains
                .filter((d) => !wsSearch || d.name.toLowerCase().includes(wsSearch.toLowerCase()))
                .map((d) => (
                  <button
                    key={d.id}
                    data-testid={`domain-item-${d.id}`}
                    onClick={() => { setActiveDomain(d.id); newThread(); setSelectorOpen(false) }}
                    className={`w-full rounded-sm px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent hover:text-accent-foreground ${
                      d.id === activeDomainId ? "font-medium bg-accent" : ""
                    }`}
                  >
                    {d.name}
                  </button>
                ))}
              {domains.length > 0 &&
                wsSearch &&
                !domains.some((d) => d.name.toLowerCase().includes(wsSearch.toLowerCase())) && (
                  <p className="px-2 py-4 text-center text-sm text-muted-foreground">
                    No workspaces match.
                  </p>
                )}
            </div>
            {/* Pinned footer actions */}
            <div className="border-t p-1">
              <button
                onClick={() => { setSelectorOpen(false); navigate(`${pathPrefix}/workspaces`) }}
                className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <Settings className="h-3.5 w-3.5" />
                Manage workspaces
              </button>
              <button
                onClick={() => { setSelectorOpen(false); setTimeout(() => setShowCreateModal(true), 0) }}
                className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
              >
                <Plus className="h-3.5 w-3.5" />
                New workspace
              </button>
            </div>
          </PopoverContent>
        </Popover>
        {showCreateModal && (
          <CreateWorkspaceModal onClose={() => setShowCreateModal(false)} />
        )}
      </div>

      {/* Navigation */}
      <nav className="space-y-1 p-4">
        <NavItem to={`${pathPrefix}/`} icon={MessageSquare} label="Chat" />
        <NavItem to={`${pathPrefix}/artifacts`} icon={LayoutDashboard} label="Artifacts" />
        <NavItem to={`${pathPrefix}/knowledge`} icon={BookOpen} label="Knowledge" />
        <NavItem to={`${pathPrefix}/recipes`} icon={ChefHat} label="Recipes" />
        <NavItem to={`${pathPrefix}/data-dictionary`} icon={Database} label="Data Dictionary" />
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
            onClick={() => { newThread(); navigate(`${pathPrefix}/chat`) }}
            data-testid="sidebar-new-chat"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {threads.map((thread) => (
            <button
              key={thread.id}
              onClick={() => { selectThread(thread.id); navigate(`${pathPrefix}/chat`) }}
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
          <Link to={`${pathPrefix}/settings/connections`}>
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
