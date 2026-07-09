import { useEffect } from "react"
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
  Loader2,
} from "lucide-react"
import { useAppStore } from "@/store/store"
import { useWorkspaceJobs } from "@/contexts/WorkspaceJobsContext"
import { workspacePath } from "@/lib/workspacePath"
import { NavItem } from "./NavItem"
import { Button } from "@/components/ui/button"
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher"

export function Sidebar() {
  const navigate = useNavigate()
  const user = useAppStore((s) => s.user)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const domains = useAppStore((s) => s.domains)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const logout = useAppStore((s) => s.authActions.logout)
  const threadId = useAppStore((s) => s.threadId)
  const threads = useAppStore((s) => s.threads)
  const threadsStatus = useAppStore((s) => s.threadsStatus)
  const threadsAccessLostMessage = useAppStore((s) => s.threadsAccessLostMessage)
  const fetchThreads = useAppStore((s) => s.uiActions.fetchThreads)
  const newThread = useAppStore((s) => s.uiActions.newThread)
  const selectThread = useAppStore((s) => s.uiActions.selectThread)
  const { jobsByThreadId, recentlyCompletedThreadIds } = useWorkspaceJobs()
  const location = useLocation()
  const isEmbed = location.pathname.startsWith("/embed")
  const pathPrefix = isEmbed ? "/embed" : ""

  // Pretty chat base for the active workspace: `${pathPrefix}/workspaces/<slug>/<uuid>/chat`.
  // Falls back to the bare `/workspaces/<uuid>/chat` until the workspace is found
  // in `domains` (workspacePath degrades to bare when no name is available).
  const activeWorkspace = domains.find((d) => d.id === activeDomainId)
  const chatBase = activeDomainId
    ? `${pathPrefix}${workspacePath(activeWorkspace ?? { id: activeDomainId })}/chat`
    : null

  useEffect(() => {
    fetchDomains()
  }, [fetchDomains])

  useEffect(() => {
    if (activeDomainId) {
      fetchThreads(activeDomainId)
    }
  }, [activeDomainId, fetchThreads])

  // Refetch threads when jobs complete so the sidebar green-dot indicator
  // picks up the bumped Thread.updated_at from the resume task.
  useEffect(() => {
    if (activeDomainId && recentlyCompletedThreadIds.length > 0) {
      void fetchThreads(activeDomainId)
    }
  }, [recentlyCompletedThreadIds, activeDomainId, fetchThreads])

  return (
    <aside className="flex h-screen w-64 flex-col border-r bg-background">
      {/* Logo — height matches the TopBar (h-11) so their bottom borders align */}
      <div className="flex h-11 items-center border-b px-4">
        <Link to={`${pathPrefix}/`} className="flex items-center gap-2 font-semibold">
          <span className="text-lg">Scout</span>
        </Link>
      </div>

      {/* Workspace Selector — only in embed mode, which has no TopBar.
          Outside embed, the workspace switcher lives in the top-right TopBar. */}
      {isEmbed && (
        <div className="border-b p-4">
          <label className="text-xs font-medium text-muted-foreground">Workspace</label>
          <WorkspaceSwitcher />
        </div>
      )}

      <nav className="space-y-1 p-4">
        <NavItem
          to={chatBase ?? `${pathPrefix}/`}
          icon={MessageSquare}
          label="Chat"
          isActivePath={(p) => /\/workspaces\/(?:[^/]+\/)?[^/]+\/chat(\/|$)/.test(p)}
        />
        <NavItem to={`${pathPrefix}/artifacts`} icon={LayoutDashboard} label="Artifacts" />
        <NavItem to={`${pathPrefix}/knowledge`} icon={BookOpen} label="Knowledge" />
        <NavItem to={`${pathPrefix}/recipes`} icon={ChefHat} label="Recipes" />
        <NavItem to={`${pathPrefix}/data-dictionary`} icon={Database} label="Data Dictionary" />
      </nav>

      <div className="flex flex-1 flex-col border-t overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2">
          <span className="text-xs font-medium text-muted-foreground">
            Chat History
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={() => {
              newThread()
              navigate(chatBase ?? `${pathPrefix}/chat`)
            }}
            data-testid="sidebar-new-chat"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {threadsStatus === "error" && threadsAccessLostMessage && (
            // Lost upstream tenant access: retry won't help, so explain what
            // happened and what to do instead of offering a dead retry.
            <div
              className="px-3 py-2 text-xs text-muted-foreground"
              data-testid="sidebar-threads-access-lost"
            >
              <p>{threadsAccessLostMessage}</p>
            </div>
          )}
          {threadsStatus === "error" && !threadsAccessLostMessage && (
            // 07#7: a load failure must not look like "no conversations". Show a
            // distinct error + retry so an outage is recoverable, not silent.
            <div
              className="px-3 py-2 text-xs text-muted-foreground"
              data-testid="sidebar-threads-error"
            >
              <p>Couldn&apos;t load conversations.</p>
              <button
                type="button"
                onClick={() => {
                  if (activeDomainId) void fetchThreads(activeDomainId)
                }}
                className="mt-1 text-primary underline-offset-2 hover:underline"
                data-testid="sidebar-threads-retry"
              >
                Retry
              </button>
            </div>
          )}
          {threads.map((thread) => {
            const job = jobsByThreadId[thread.id]
            const lastUpdated = new Date(thread.updated_at)
            const baseline = thread.last_viewed_at
              ? new Date(thread.last_viewed_at)
              : new Date(thread.created_at)
            const hasUnread = lastUpdated > baseline
            return (
              <button
                key={thread.id}
                onClick={() => {
                  selectThread(thread.id)
                  navigate(chatBase ? `${chatBase}/${thread.id}` : `${pathPrefix}/chat`)
                }}
                data-testid={`sidebar-thread-${thread.id}`}
                className={`flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm transition-colors ${
                  thread.id === threadId
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                }`}
              >
                <span className="flex-1 truncate">{thread.title}</span>
                {job ? (
                  <span
                    className="flex items-center gap-1 text-xs"
                    data-testid={`sidebar-thread-job-${thread.id}`}
                    title={
                      job.progress?.source
                        ? `Loading ${job.progress.source}${job.progress.rows_loaded ? ` — ${job.progress.rows_loaded.toLocaleString()} rows` : ""}`
                        : (job.progress?.message ?? "Materializing...")
                    }
                  >
                    <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />
                    {job.progress?.percent != null ? (
                      <span
                        className="font-medium text-primary"
                        data-testid={`sidebar-thread-job-percent-${thread.id}`}
                      >
                        {job.progress.percent}%
                      </span>
                    ) : job.progress?.source ? (
                      <span
                        className="truncate max-w-[4rem]"
                        data-testid={`sidebar-thread-job-source-${thread.id}`}
                      >
                        {job.progress.source}
                      </span>
                    ) : null}
                  </span>
                ) : hasUnread ? (
                  <span
                    className="h-2 w-2 rounded-full bg-green-500"
                    data-testid={`sidebar-thread-unread-${thread.id}`}
                  />
                ) : null}
              </button>
            )
          })}
        </div>
      </div>

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
