import { useEffect, useMemo, useRef, useState } from "react"
import { useNavigate, useLocation } from "react-router-dom"
import { Check, ChevronDown, Loader2, Plus, Search, Settings } from "lucide-react"
import { useAppStore } from "@/store/store"
import { workspaceDataState, workspaceHasData } from "@/api/workspaces"
import type { TenantMembership } from "@/store/domainSlice"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { getRecentWorkspaceIds, recordWorkspaceUse } from "@/lib/recentWorkspaces"
import { formatRelativeTime } from "@/lib/relativeTime"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"

// Row windowing: when the visible list is large, only render rows near the
// viewport so 270 workspaces stay smooth without a virtualization dependency.
const ROW_HEIGHT = 36 // px, matches a single list row
const LIST_MAX_HEIGHT = 288 // px (max-h-72)
const WINDOW_OVERSCAN = 6
const WINDOW_THRESHOLD = 60 // below this, render everything (cheap)
const RECENT_LIMIT = 8
const SEGMENT_MIN_WORKSPACES = 12 // hide segmented controls for small accounts

type SegmentKey = "recent" | "all" | string // string => a provider id

function firstProvider(ws: TenantMembership): string | undefined {
  return ws.tenants?.[0]?.provider
}

function hasProvider(ws: TenantMembership, provider: string): boolean {
  return (ws.tenants ?? []).some((t) => t.provider === provider)
}

interface RowProps {
  ws: TenantMembership
  active: boolean
  highlighted: boolean
  onSelect: () => void
  onHover: () => void
  onSettings: () => void
}

/**
 * Live, three-state data indicator. Reflects the workspace's current
 * `schema_status` (not the historical `last_synced_at`):
 *   - loading → spinner ("Loading data…")
 *   - ready   → emerald dot ("Has data" + relative sync time when known)
 *   - empty   → hollow dot ("No data")
 */
function DataIndicator({ ws }: { ws: TenantMembership }) {
  const state = workspaceDataState(ws)
  const label =
    state === "loading"
      ? "Loading data…"
      : state === "ready"
        ? `Has data${ws.last_synced_at ? ` — synced ${formatRelativeTime(ws.last_synced_at)}` : ""}`
        : "No data"

  return (
    <span
      title={label}
      aria-label={label}
      role="img"
      data-testid={`workspace-data-indicator-${ws.id}`}
      data-data-state={state}
      className="flex h-3.5 w-3.5 shrink-0 items-center justify-center"
    >
      {state === "loading" ? (
        <Loader2 className="h-3 w-3 animate-spin text-primary" aria-hidden />
      ) : (
        <span
          className={cn(
            "h-2 w-2 rounded-full",
            state === "ready"
              ? "bg-emerald-500"
              : "border border-muted-foreground/40 bg-transparent",
          )}
          aria-hidden
        />
      )}
    </span>
  )
}

function WorkspaceRow({ ws, active, highlighted, onSelect, onHover, onSettings }: RowProps) {
  const { Icon } = getProviderMeta(firstProvider(ws))
  const dataState = workspaceDataState(ws)

  // The gear lives inside the row button, so it can't itself be a <button>
  // (no nested interactive elements). A role="button" span with keyboard
  // handling keeps the markup valid while staying accessible.
  function handleSettings(e: React.MouseEvent | React.KeyboardEvent) {
    e.stopPropagation()
    e.preventDefault()
    onSettings()
  }

  return (
    <button
      data-testid={`domain-item-${ws.id}`}
      data-has-data={dataState === "ready"}
      data-data-state={dataState}
      onClick={onSelect}
      onMouseMove={onHover}
      style={{ height: ROW_HEIGHT }}
      className={cn(
        "flex w-full items-center gap-2 rounded-sm px-2 text-left text-sm transition-colors",
        highlighted ? "bg-accent text-accent-foreground" : "hover:bg-accent hover:text-accent-foreground",
        active && "font-medium",
      )}
    >
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
      <span className="flex-1 truncate">{ws.display_name}</span>
      <DataIndicator ws={ws} />
      <span
        role="button"
        tabIndex={0}
        data-testid={`workspace-switcher-settings-${ws.id}`}
        aria-label={`Manage ${ws.display_name}`}
        title="Manage workspace"
        onClick={handleSettings}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") handleSettings(e)
        }}
        className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-sm text-muted-foreground/60 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <Settings className="h-3.5 w-3.5" aria-hidden />
      </span>
      {active ? (
        <Check className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
      ) : (
        <span className="h-3.5 w-3.5 shrink-0" aria-hidden />
      )}
    </button>
  )
}

export function WorkspaceSwitcher() {
  const navigate = useNavigate()
  const location = useLocation()
  const pathPrefix = location.pathname.startsWith("/embed") ? "/embed" : ""

  const domains = useAppStore((s) => s.domains)
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const newThread = useAppStore((s) => s.uiActions.newThread)

  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState("")
  const [segment, setSegment] = useState<SegmentKey>("recent")
  const [hasDataOnly, setHasDataOnly] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [showCreateModal, setShowCreateModal] = useState(false)
  // Snapshot of recent ids taken when the popover opens, so selecting a
  // workspace (which writes localStorage) doesn't reshuffle the list mid-use.
  const [recentIds, setRecentIds] = useState<string[]>([])

  const scrollRef = useRef<HTMLDivElement>(null)
  const [scrollTop, setScrollTop] = useState(0)

  const activeWorkspace = domains.find((d) => d.id === activeDomainId)

  // Providers present across the user's workspaces, ordered by count desc.
  const providers = useMemo(() => {
    const counts = new Map<string, number>()
    for (const ws of domains) {
      for (const p of new Set((ws.tenants ?? []).map((t) => t.provider))) {
        counts.set(p, (counts.get(p) ?? 0) + 1)
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([value, count]) => ({ value, count }))
  }, [domains])

  // Segmented controls only earn their space when there's enough to navigate.
  const showSegments = domains.length >= SEGMENT_MIN_WORKSPACES || providers.length > 1

  // Recent workspaces (newest first), filtered to ones the user still has.
  const recent = useMemo(() => {
    const byId = new Map(domains.map((d) => [d.id, d]))
    return recentIds
      .map((id) => byId.get(id))
      .filter((d): d is TenantMembership => d != null)
      .slice(0, RECENT_LIMIT)
  }, [domains, recentIds])

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    const matchesSearch = (ws: TenantMembership) =>
      !q ||
      ws.display_name.toLowerCase().includes(q) ||
      (ws.tenants ?? []).some((t) => t.tenant_name.toLowerCase().includes(q))

    // When searching, ignore the Recent segment and search across everything.
    let base: TenantMembership[]
    if (q) {
      base = domains
    } else if (segment === "recent") {
      base = recent
    } else if (segment === "all") {
      base = domains
    } else {
      base = domains.filter((ws) => hasProvider(ws, segment))
    }

    let list = base.filter(matchesSearch)
    if (hasDataOnly) list = list.filter(workspaceHasData)

    // Recent is intentionally insertion-ordered; everything else alphabetical.
    if (q || segment !== "recent") {
      list = [...list].sort((a, b) => a.display_name.localeCompare(b.display_name))
    }
    return list
  }, [domains, recent, search, segment, hasDataOnly])

  // Reset highlight + scroll position whenever the visible set changes.
  function resetView() {
    setHighlight(0)
    if (scrollRef.current) scrollRef.current.scrollTop = 0
    setScrollTop(0)
  }

  function changeSearch(value: string) {
    setSearch(value)
    resetView()
  }

  function changeSegment(key: SegmentKey) {
    setSearch("")
    setSegment(key)
    resetView()
  }

  function toggleHasData() {
    setHasDataOnly((v) => !v)
    resetView()
  }

  function close() {
    setOpen(false)
    setSearch("")
  }

  function select(ws: TenantMembership) {
    recordWorkspaceUse(ws.id)
    setActiveDomain(ws.id)
    newThread()
    close()
  }

  function manage(ws: TenantMembership) {
    close()
    navigate(`${pathPrefix}/workspaces/${ws.id}`)
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault()
      setHighlight((h) => Math.min(h + 1, visible.length - 1))
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setHighlight((h) => Math.max(h - 1, 0))
    } else if (e.key === "Enter") {
      e.preventDefault()
      const ws = visible[highlight] ?? visible[0]
      if (ws) select(ws)
    } else if (e.key === "Escape") {
      close()
    }
  }

  // Keep the highlighted row in view as the user arrows through.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const top = highlight * ROW_HEIGHT
    const bottom = top + ROW_HEIGHT
    if (top < el.scrollTop) el.scrollTop = top
    else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight
  }, [highlight])

  // Windowing math.
  const windowed = visible.length > WINDOW_THRESHOLD
  const startIndex = windowed
    ? Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - WINDOW_OVERSCAN)
    : 0
  const visibleCount = windowed
    ? Math.ceil(LIST_MAX_HEIGHT / ROW_HEIGHT) + WINDOW_OVERSCAN * 2
    : visible.length
  const endIndex = Math.min(visible.length, startIndex + visibleCount)
  const slice = visible.slice(startIndex, endIndex)

  const segmentLabel = (key: SegmentKey) => {
    if (key === "recent") return "Recent"
    if (key === "all") return "All"
    return getProviderMeta(key).label
  }
  const segmentCount = (key: SegmentKey): number | null => {
    if (key === "recent") return recent.length || null
    if (key === "all") return domains.length
    return providers.find((p) => p.value === key)?.count ?? null
  }

  const segmentKeys: SegmentKey[] = showSegments
    ? ["recent", "all", ...providers.map((p) => p.value)]
    : []

  return (
    <>
      <Popover
        open={open}
        onOpenChange={(o) => {
          setOpen(o)
          if (o) {
            const ids = getRecentWorkspaceIds()
            setRecentIds(ids)
            setSegment(ids.length > 0 ? "recent" : "all")
            resetView()
          } else {
            setSearch("")
          }
        }}
      >
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            className="mt-1 w-full justify-between font-normal"
            data-testid="domain-selector"
          >
            <span className="flex min-w-0 items-center gap-2">
              {activeWorkspace ? (
                (() => {
                  const { Icon } = getProviderMeta(firstProvider(activeWorkspace))
                  return <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
                })()
              ) : null}
              <span className="truncate">
                {activeWorkspace?.display_name ?? "Select workspace"}
              </span>
            </span>
            <ChevronDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[22rem] p-0" align="start" onKeyDown={onKeyDown}>
          {/* Search */}
          <div className="border-b p-2">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                autoFocus
                placeholder="Search workspaces..."
                value={search}
                onChange={(e) => changeSearch(e.target.value)}
                className="h-8 pl-7 text-sm"
                data-testid="workspace-search"
              />
            </div>
          </div>

          {/* Filter controls. Pills wrap instead of scrolling sideways, so no
              provider is ever clipped; "Has data" is a compact icon toggle
              pinned to the right so it never competes for horizontal space. */}
          {showSegments && (
            <div className="flex items-start gap-2 border-b px-2 py-1.5">
              <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
                {segmentKeys.map((key) => {
                  const count = segmentCount(key)
                  const isActive = !search && segment === key
                  return (
                    <button
                      key={key}
                      data-testid={`workspace-seg-${key}`}
                      onClick={() => changeSegment(key)}
                      className={cn(
                        "max-w-full truncate rounded-full px-2 py-0.5 text-xs transition-colors",
                        isActive
                          ? "bg-primary text-primary-foreground"
                          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                      )}
                    >
                      {segmentLabel(key)}
                      {count != null && <span className="ml-1 opacity-60">{count}</span>}
                    </button>
                  )
                })}
              </div>
              <button
                data-testid="workspace-filter-hasdata"
                aria-pressed={hasDataOnly}
                aria-label={
                  hasDataOnly ? "Showing only workspaces with data" : "Show only workspaces with data"
                }
                onClick={toggleHasData}
                title="Show only workspaces with data"
                className={cn(
                  "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border transition-colors",
                  hasDataOnly
                    ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                    : "border-transparent text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                )}
              >
                <span
                  className={cn(
                    "h-2 w-2 rounded-full",
                    hasDataOnly ? "bg-emerald-500" : "border border-current bg-transparent",
                  )}
                />
              </button>
            </div>
          )}

          {/* List */}
          <div
            ref={scrollRef}
            onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
            className="overflow-y-auto p-1"
            style={{ maxHeight: LIST_MAX_HEIGHT }}
            data-testid="workspace-list"
          >
            {visible.length === 0 ? (
              <p className="px-2 py-6 text-center text-sm text-muted-foreground">
                {hasDataOnly
                  ? "No workspaces with data yet."
                  : !search && segment === "recent"
                    ? "No recent workspaces — pick one to get started."
                    : "No workspaces match."}
              </p>
            ) : (
              <div style={{ height: windowed ? visible.length * ROW_HEIGHT : undefined, position: "relative" }}>
                <div
                  style={
                    windowed
                      ? { position: "absolute", top: startIndex * ROW_HEIGHT, left: 0, right: 0 }
                      : undefined
                  }
                >
                  {slice.map((ws, i) => {
                    const index = startIndex + i
                    return (
                      <WorkspaceRow
                        key={ws.id}
                        ws={ws}
                        active={ws.id === activeDomainId}
                        highlighted={index === highlight}
                        onSelect={() => select(ws)}
                        onHover={() => setHighlight(index)}
                        onSettings={() => manage(ws)}
                      />
                    )
                  })}
                </div>
              </div>
            )}
          </div>

          {/* Footer actions */}
          <div className="border-t p-1">
            <button
              data-testid="workspace-manage"
              onClick={() => {
                close()
                navigate(`${pathPrefix}/workspaces`)
              }}
              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <Settings className="h-3.5 w-3.5" />
              Manage workspaces
            </button>
            <button
              data-testid="workspace-new"
              onClick={() => {
                close()
                setTimeout(() => setShowCreateModal(true), 0)
              }}
              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <Plus className="h-3.5 w-3.5" />
              New workspace
            </button>
          </div>
        </PopoverContent>
      </Popover>

      {showCreateModal && <CreateWorkspaceModal onClose={() => setShowCreateModal(false)} />}
    </>
  )
}
