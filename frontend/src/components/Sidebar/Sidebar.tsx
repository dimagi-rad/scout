import { useCallback, useEffect, useRef, useState } from "react"
import { Link, useLocation, useNavigate } from "react-router-dom"
import {
  MessageSquare,
  BookOpen,
  Brain,
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
import { isWorkspaceArtifactPath } from "@/lib/artifactPath"
import { NavItem } from "./NavItem"
import { Button } from "@/components/ui/button"
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher"
import { CONNECTIONS_PATH } from "@/lib/routes"
import type { AccessDenialReason } from "@/store/uiSlice"

// The server's message names every source and remedy, too long for the sidebar;
// the lost-access modal and Connected Accounts carry the detail.
const ACCESS_DENIAL_SUMMARY: Record<AccessDenialReason, string> = {
  tenant_access_lost: "You don't have access to all of this workspace's sources.",
  upstream_access_lost: "A provider removed your access to one of this workspace's sources.",
  credential_missing: "One of this workspace's sources isn't connected.",
  credential_expired: "Your sign-in for one of this workspace's sources expired.",
  verification_indeterminate: "A provider gave an answer Scout couldn't read.",
  verification_unavailable: "Couldn't verify your access right now.",
  verification_in_progress: "Your access is still being verified.",
  no_sources: "This workspace has no data sources.",
}

// Focus and visibilitychange both fire on a tab switch, and alt-tabbing fires focus often.
const DOMAIN_REVALIDATE_MIN_INTERVAL_MS = 15_000
// Catches a grant while you stay on the tab. Slow on purpose: prod and staging share one RDS.
const DOMAIN_REVALIDATE_POLL_MS = 60_000
// While a listed thread's turn runs, refetch so its spinner clears when it ends; a
// turn's own end refetches too, but can land just before the server lets go of it.
export const RUNNING_THREADS_POLL_MS = 5_000
export const RUNNING_THREADS_MAX_POLL_MS = 30_000

export function Sidebar() {
  const navigate = useNavigate()
  const sidebarRef = useRef<HTMLDivElement>(null)
  const [isRailExpanded, setIsRailExpanded] = useState(false)
  const [isTouchDevice, setIsTouchDevice] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(hover: none)").matches
      : false
  )
  const user = useAppStore((s) => s.user)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const domains = useAppStore((s) => s.domains)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const revalidateDomains = useAppStore((s) => s.domainActions.revalidateDomains)
  const logout = useAppStore((s) => s.authActions.logout)
  const threadId = useAppStore((s) => s.threadId)
  const threads = useAppStore((s) => s.threads)
  const threadsStatus = useAppStore((s) => s.threadsStatus)
  const threadsAccessDenialReason = useAppStore((s) => s.threadsAccessDenialReason)
  const threadsAccessRetryable = useAppStore((s) => s.threadsAccessRetryable)
  const retryAccessVerification = useAppStore((s) => s.uiActions.retryAccessVerification)
  const [verifyingWorkspaceId, setVerifyingWorkspaceId] = useState<string | null>(null)
  const retryingVerification = verifyingWorkspaceId !== null && verifyingWorkspaceId === activeDomainId
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

  const collapseSidebar = () => {
    if (typeof document !== "undefined" && document.activeElement instanceof HTMLElement) {
      document.activeElement.blur()
    }
    window.requestAnimationFrame(() => {
      const isHovering =
        !isTouchDevice &&
        document.visibilityState !== "hidden" &&
        (sidebarRef.current?.matches(":hover") ?? false)
      setIsRailExpanded(isHovering)
    })
  }

  const syncExpandedToInteractionState = useCallback(() => {
    const sidebar = sidebarRef.current
    if (!sidebar) return

    const activeElement = document.activeElement
    const focusInside = activeElement ? sidebar.contains(activeElement) : false
    const hoverInside =
      !isTouchDevice &&
      document.visibilityState !== "hidden" &&
      sidebar.matches(":hover")
    const shouldExpand = hoverInside || focusInside

    setIsRailExpanded((current) => (current === shouldExpand ? current : shouldExpand))
  }, [isTouchDevice])

  const expandFromPointer = () => {
    if (!isTouchDevice && document.visibilityState !== "hidden") {
      setIsRailExpanded(true)
    }
  }

  const collapseFromPointer = () => {
    const sidebar = sidebarRef.current
    const activeElement = document.activeElement
    const focusInside =
      sidebar && activeElement ? sidebar.contains(activeElement) : false
    if (!focusInside) {
      setIsRailExpanded(false)
    }
  }

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return undefined
    }

    const mediaQuery = window.matchMedia("(hover: none)")
    const handleChange = (event: MediaQueryListEvent) => {
      setIsTouchDevice(event.matches)
    }

    mediaQuery.addEventListener("change", handleChange)
    return () => {
      mediaQuery.removeEventListener("change", handleChange)
    }
  }, [])

  useEffect(() => {
    const intervalId = window.setInterval(syncExpandedToInteractionState, 100)
    const handleDocumentMouseOut = (event: MouseEvent) => {
      if (event.relatedTarget === null) syncExpandedToInteractionState()
    }

    window.addEventListener("blur", syncExpandedToInteractionState)
    window.addEventListener("focus", syncExpandedToInteractionState)
    document.addEventListener("visibilitychange", syncExpandedToInteractionState)
    document.addEventListener("mouseout", handleDocumentMouseOut)

    return () => {
      window.clearInterval(intervalId)
      window.removeEventListener("blur", syncExpandedToInteractionState)
      window.removeEventListener("focus", syncExpandedToInteractionState)
      document.removeEventListener("visibilitychange", syncExpandedToInteractionState)
      document.removeEventListener("mouseout", handleDocumentMouseOut)
    }
  }, [syncExpandedToInteractionState])

  // Fetch domains on mount
  useEffect(() => {
    fetchDomains()
  }, [fetchDomains])

  // Workspaces someone else added you to stay invisible until the list is
  // fetched again (#355): on coming back to the tab, and on a slow poll while you stay.
  const lastRevalidatedAtRef = useRef(0)
  useEffect(() => {
    const revalidate = (fresh: boolean) => {
      if (document.visibilityState === "hidden") return
      const now = Date.now()
      if (now - lastRevalidatedAtRef.current < DOMAIN_REVALIDATE_MIN_INTERVAL_MS) return
      const previous = lastRevalidatedAtRef.current
      lastRevalidatedAtRef.current = now
      // Fresh on return: a request from before you came back can't show a workspace added
      // meanwhile. A poll tick has no such moment, so it joins any request already in flight.
      void revalidateDomains({ fresh }).then((result) => {
        // A skip left the list to another load, so it mustn't use up the window for a real return.
        if (result === "skipped" && lastRevalidatedAtRef.current === now) {
          lastRevalidatedAtRef.current = previous
        }
      })
    }
    const revalidateOnReturn = () => revalidate(true)
    document.addEventListener("visibilitychange", revalidateOnReturn)
    window.addEventListener("focus", revalidateOnReturn)
    const pollId = window.setInterval(() => revalidate(false), DOMAIN_REVALIDATE_POLL_MS)
    return () => {
      window.clearInterval(pollId)
      document.removeEventListener("visibilitychange", revalidateOnReturn)
      window.removeEventListener("focus", revalidateOnReturn)
    }
  }, [revalidateDomains])

  // Fetch threads when domain changes
  useEffect(() => {
    if (activeDomainId) {
      fetchThreads(activeDomainId)
    }
  }, [activeDomainId, fetchThreads])

  const localTurnThreadIds = useAppStore((s) => s.localTurnThreadIds)
  // This tab's own turns end with a refetch of their own, so only others are polled for.
  const runningThreadIds = threads
    .filter((thread) => thread.turn_running && !localTurnThreadIds.has(thread.id))
    .map((thread) => thread.id)
    .sort()
    .join(",")
  useEffect(() => {
    // A denial keeps the old list (still running); refetching it would only repeat the denial.
    if (!activeDomainId || !runningThreadIds || threadsAccessDenialReason) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let delay = RUNNING_THREADS_POLL_MS
    const schedule = () => {
      timer = setTimeout(async () => {
        // Hidden ticks fetch nothing, so they earn no backoff.
        const hidden = document.hidden
        if (!hidden) await fetchThreads(activeDomainId)
        if (cancelled) return
        delay = hidden
          ? RUNNING_THREADS_POLL_MS
          : Math.min(delay * 1.5, RUNNING_THREADS_MAX_POLL_MS)
        schedule()
      }, delay)
    }
    schedule()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [activeDomainId, runningThreadIds, threadsAccessDenialReason, fetchThreads])

  // Refetch threads when jobs complete so the sidebar green-dot indicator
  // picks up the bumped Thread.updated_at from the resume task.
  useEffect(() => {
    if (activeDomainId && recentlyCompletedThreadIds.length > 0) {
      void fetchThreads(activeDomainId)
    }
  }, [recentlyCompletedThreadIds, activeDomainId, fetchThreads])

  return (
    <div
      ref={sidebarRef}
      className="scout-sidebar-shell"
      data-expanded={isRailExpanded ? "true" : "false"}
      data-testid="sidebar-shell"
      onPointerEnter={expandFromPointer}
      onPointerMove={expandFromPointer}
      onPointerLeave={collapseFromPointer}
      onPointerCancel={collapseFromPointer}
      onFocusCapture={() => setIsRailExpanded(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          setIsRailExpanded(false)
        }
      }}
    >
      <aside
        className="scout-sidebar-panel flex h-screen flex-col overflow-hidden border-r bg-background"
        aria-label="Primary navigation"
      >
        {/* Logo — height matches the TopBar (h-11) so their bottom borders align */}
        <div className="flex h-11 items-center border-b px-3 lg:px-4">
          <Link
            to={`${pathPrefix}/`}
            className="scout-sidebar-brand flex min-w-0 items-center gap-2 font-semibold"
            title="Scout"
            onClick={collapseSidebar}
          >
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary text-xs font-semibold text-primary-foreground">
              S
            </span>
            <span className="scout-sidebar-label min-w-0 truncate text-lg">Scout</span>
            {user?.agent_model && (
              <span
                data-testid="app-model-label"
                title={user.agent_model.id}
                className="scout-sidebar-label min-w-0 shrink truncate pt-1 text-xs font-normal text-muted-foreground"
              >
                {user.agent_model.label}
              </span>
            )}
          </Link>
        </div>

        {/* Workspace Selector — only in embed mode, which has no TopBar.
            Outside embed, the workspace switcher lives in the top-right TopBar. */}
        {isEmbed && (
          <div className="scout-sidebar-expanded-block border-b p-4">
            <label className="text-xs font-medium text-muted-foreground">Workspace</label>
            <WorkspaceSwitcher />
          </div>
        )}

        {/* Navigation */}
        <nav className="space-y-1 p-3 lg:p-4">
          <NavItem
            to={chatBase ?? `${pathPrefix}/`}
            icon={MessageSquare}
            label="Chat"
            onNavigate={collapseSidebar}
            isActivePath={(p) => /\/workspaces\/(?:[^/]+\/)?[^/]+\/chat(\/|$)/.test(p)}
          />
          <NavItem
            to={`${pathPrefix}/artifacts`}
            icon={LayoutDashboard}
            label="Artifacts"
            onNavigate={collapseSidebar}
            isActivePath={isWorkspaceArtifactPath}
          />
          <NavItem
            to={`${pathPrefix}/knowledge`}
            icon={BookOpen}
            label="Knowledge"
            onNavigate={collapseSidebar}
          />
          <NavItem
            to={`${pathPrefix}/memory`}
            icon={Brain}
            label="Memory"
            onNavigate={collapseSidebar}
          />
          <NavItem
            to={`${pathPrefix}/recipes`}
            icon={ChefHat}
            label="Recipes"
            onNavigate={collapseSidebar}
          />
          <NavItem
            to={`${pathPrefix}/datasets`}
            icon={Database}
            label="Datasets"
            onNavigate={collapseSidebar}
          />
        </nav>

        {/* Thread History */}
        <div className="flex flex-1 flex-col overflow-hidden border-t">
          <div className="scout-sidebar-history-header flex items-center justify-center px-3 py-2 lg:justify-between lg:px-4">
            <span className="scout-sidebar-label text-xs font-medium text-muted-foreground">
              Chat History
            </span>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 shrink-0"
              onClick={() => {
                newThread()
                collapseSidebar()
                navigate(chatBase ?? `${pathPrefix}/chat`)
              }}
              title="New chat"
              data-testid="sidebar-new-chat"
            >
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </div>
          <div className="scout-sidebar-expanded-block flex-1 overflow-y-auto px-2 pb-2">
            {threadsStatus === "error" && threadsAccessDenialReason && (
              // The lost-access modal skips recovery pages and transient denials; this must not.
              <div
                className="px-3 py-2 text-xs text-muted-foreground"
                data-testid="sidebar-threads-access-lost"
              >
                <p>{ACCESS_DENIAL_SUMMARY[threadsAccessDenialReason]}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {threadsAccessRetryable && (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={retryingVerification}
                      onClick={() => {
                        if (!activeDomainId) return
                        const workspaceId = activeDomainId
                        setVerifyingWorkspaceId(workspaceId)
                        void retryAccessVerification(workspaceId).finally(() =>
                          setVerifyingWorkspaceId((current) =>
                            current === workspaceId ? null : current,
                          ),
                        )
                      }}
                      data-testid="sidebar-threads-retry-verification"
                    >
                      {retryingVerification ? "Verifying…" : "Retry verification"}
                    </Button>
                  )}
                  {threadsAccessDenialReason !== "no_sources" && (
                    <Button variant="outline" size="xs" asChild>
                      <Link
                        to={`${pathPrefix}/settings/connections`}
                        onClick={collapseSidebar}
                        data-testid="sidebar-threads-connected-accounts"
                      >
                        Connected Accounts
                      </Link>
                    </Button>
                  )}
                </div>
              </div>
            )}
            {threadsStatus === "error" && !threadsAccessDenialReason && (
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
              const historyTitle = sidebarThreadTitle(thread)
              const job = jobsByThreadId[thread.id]
              const lastUpdated = new Date(thread.updated_at)
              const baseline = thread.last_viewed_at
                ? new Date(thread.last_viewed_at)
                : new Date(thread.created_at)
              const hasUnread = lastUpdated > baseline
              const turnRunning = thread.turn_running || localTurnThreadIds.has(thread.id)
              return (
                <button
                  key={thread.id}
                  onClick={() => {
                    selectThread(thread.id)
                    collapseSidebar()
                    navigate(chatBase ? `${chatBase}/${thread.id}` : `${pathPrefix}/chat`)
                  }}
                  title={historyTitle}
                  data-testid={`sidebar-thread-${thread.id}`}
                  className={`flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm transition-colors ${
                    thread.id === threadId
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                  }`}
                >
                  <span className="flex-1 truncate">{historyTitle}</span>
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
                  ) : turnRunning ? (
                    <span
                      className="flex items-center"
                      title="Working on a reply"
                      data-testid={`sidebar-thread-running-${thread.id}`}
                    >
                      <Loader2 className="h-3 w-3 animate-spin text-primary shrink-0" />
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

        {/* User Section */}
        <div className="border-t p-3 lg:p-4">
          <div className="scout-sidebar-expanded-block mb-2 truncate text-sm text-muted-foreground">
            {user?.email}
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="scout-sidebar-nav-link w-full px-2 lg:px-3"
            asChild
            title="Connected Accounts"
            data-testid="sidebar-connections"
          >
            <Link to={`${pathPrefix}${CONNECTIONS_PATH}`} onClick={collapseSidebar}>
              <Link2 className="h-4 w-4 shrink-0" />
              <span className="scout-sidebar-label min-w-0 truncate">
                Connected Accounts
              </span>
            </Link>
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="scout-sidebar-nav-link w-full px-2 lg:px-3"
            onClick={() => {
              collapseSidebar()
              logout()
            }}
            title="Logout"
            data-testid="logout-btn"
          >
            <LogOut className="h-4 w-4 shrink-0" />
            <span className="scout-sidebar-label min-w-0 truncate">Logout</span>
          </Button>
        </div>
      </aside>
    </div>
  )
}

function sidebarThreadTitle(thread: { title: string }): string {
  return thread.title.trim() || "Untitled"
}
