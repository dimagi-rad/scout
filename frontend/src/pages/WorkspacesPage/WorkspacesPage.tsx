import { useState, useMemo, useEffect } from "react"
import { Link, useLocation, useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import {
  workspaceApi,
  workspaceHasAccess,
  workspaceNeedsReconnect,
  type AwaitingInvite,
  type WorkspaceListItem,
} from "@/api/workspaces"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
import { RoleBadge } from "@/components/RoleBadge"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { getRecentWorkspaceIds } from "@/lib/recentWorkspaces"
import { workspacePath } from "@/lib/workspacePath"
import { CONNECTIONS_PATH } from "@/lib/routes"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Users, ChevronRight } from "lucide-react"
import {
  SearchFilterBar,
  type FilterGroup,
} from "@/components/SearchFilterBar/SearchFilterBar"

const providerBadgeStyles: Record<string, string> = {
  commcare: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  commcare_connect: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  ocs: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400",
}

const MAX_VISIBLE_TENANTS = 4
// Users with hundreds of workspaces (#358) got every row rendered at once.
const PAGE_SIZE = 50

type SortKey = "recent" | "newest" | "oldest" | "name"

const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: "recent", label: "Recently used" },
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
  { value: "name", label: "Name (A–Z)" },
]

function compareWorkspaces(sort: SortKey, recentIds: string[]) {
  const recentRank = new Map(recentIds.map((id, i) => [id, i]))
  return (a: WorkspaceListItem, b: WorkspaceListItem): number => {
    if (sort === "recent") {
      // Recently used first, in recency order; the rest fall back to newest first.
      const ra = recentRank.get(a.id) ?? Infinity
      const rb = recentRank.get(b.id) ?? Infinity
      if (ra !== rb) return ra < rb ? -1 : 1
      return Date.parse(b.created_at) - Date.parse(a.created_at)
    }
    if (sort === "name") {
      return (
        a.display_name.localeCompare(b.display_name, undefined, { numeric: true }) ||
        a.id.localeCompare(b.id)
      )
    }
    const byDate = Date.parse(a.created_at) - Date.parse(b.created_at)
    return sort === "oldest" ? byDate : -byDate
  }
}

function TenantList({ tenants }: { tenants: { id: string; tenant_name: string; provider: string }[] }) {
  const visible = tenants.slice(0, MAX_VISIBLE_TENANTS)
  const overflow = tenants.length - MAX_VISIBLE_TENANTS

  return (
    <div className="flex flex-wrap items-center gap-1">
      {visible.map((t) => (
        <Badge
          key={t.id}
          variant="secondary"
          className={providerBadgeStyles[t.provider] ?? "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-400"}
        >
          {t.tenant_name}
        </Badge>
      ))}
      {overflow > 0 && (
        <Badge variant="outline" className="text-xs">
          +{overflow} more
        </Badge>
      )}
    </div>
  )
}

function WorkspaceRow({ workspace, onClick }: { workspace: WorkspaceListItem; onClick: () => void }) {
  const tenants = workspace.tenants ?? []
  const { Icon } = getProviderMeta(tenants[0]?.provider)

  return (
    <button
      onClick={onClick}
      data-testid={`workspace-row-${workspace.id}`}
      className="flex w-full items-center justify-between gap-3 rounded-lg border bg-card px-4 py-3 text-left transition-colors hover:bg-accent"
    >
      <Icon className="h-5 w-5 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{workspace.display_name}</div>
        <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Users className="h-3 w-3" />
            {workspace.member_count} {workspace.member_count === 1 ? "member" : "members"}
          </span>
        </div>
        {tenants.length > 0 && (
          <div className="mt-2">
            <TenantList tenants={tenants} />
          </div>
        )}
      </div>
      <div className="flex items-center gap-3">
        {!workspaceHasAccess(workspace) && (
          <Badge
            variant="secondary"
            className="bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400"
            data-testid={`workspace-no-access-${workspace.id}`}
          >
            No access
          </Badge>
        )}
        <RoleBadge role={workspace.role} />
        <ChevronRight className="h-4 w-4 text-muted-foreground" />
      </div>
    </button>
  )
}

function AwaitingInvitesBanner() {
  const [invites, setInvites] = useState<AwaitingInvite[]>([])

  useEffect(() => {
    let cancelled = false
    // Best-effort: a failure here must not block the workspaces list.
    workspaceApi
      .getMyInvites()
      .then((data) => {
        if (!cancelled) setInvites(data)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  if (invites.length === 0) return null

  return (
    <div className="mb-6 space-y-2" data-testid="awaiting-invites-banner">
      {invites.map((invite) => (
        <div
          key={invite.id}
          className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-300"
          data-testid={`awaiting-invite-${invite.id}`}
        >
          {invite.message}
        </div>
      ))}
    </div>
  )
}

function ReconnectBanner({ count, connectionsPath }: { count: number; connectionsPath: string }) {
  return (
    <div
      className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-300"
      data-testid="workspaces-no-access-banner"
    >
      <span>
        {count} {count === 1 ? "workspace needs" : "workspaces need"} a reconnect to restore
        access.
      </span>
      <Button variant="outline" size="sm" asChild>
        <Link to={connectionsPath} data-testid="workspaces-no-access-connections">
          Open Connected Accounts
        </Link>
      </Button>
    </div>
  )
}

export function WorkspacesPage() {
  const navigate = useNavigate()
  const pathPrefix = useLocation().pathname.startsWith("/embed") ? "/embed" : ""
  const connectionsPath = `${pathPrefix}${CONNECTIONS_PATH}`
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const [showCreate, setShowCreate] = useState(false)

  const [search, setSearch] = useState("")
  const [activeFilters, setActiveFilters] = useState<Record<string, string | null>>({
    role: null,
    provider: null,
  })
  const [sort, setSort] = useState<SortKey>("newest")
  const [recentIds] = useState(getRecentWorkspaceIds)
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)

  const isLoading = domainsStatus === "loading" || domainsStatus === "idle"

  const filterGroups = useMemo((): FilterGroup[] => {
    const groups: FilterGroup[] = []

    const roleCounts = new Map<string, number>()
    for (const ws of domains) {
      roleCounts.set(ws.role, (roleCounts.get(ws.role) ?? 0) + 1)
    }
    if (roleCounts.size > 1) {
      const roleLabels: Record<string, string> = {
        read: "Read",
        read_write: "Read+Write",
        manage: "Manage",
      }
      groups.push({
        name: "role",
        options: [...roleCounts.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => ({
            value,
            label: roleLabels[value] ?? value,
            count,
          })),
      })
    }

    const providerCounts = new Map<string, number>()
    for (const ws of domains) {
      const tenants = ws.tenants ?? []
      const providers = new Set(tenants.map((t) => t.provider))
      for (const p of providers) {
        providerCounts.set(p, (providerCounts.get(p) ?? 0) + 1)
      }
    }
    // Show the provider filter whenever any workspace carries a provider, so
    // users can filter by data source even with a single provider — matching
    // the segmented "All / <provider>" control in the New Workspace modal.
    if (providerCounts.size > 0) {
      groups.push({
        name: "provider",
        options: [...providerCounts.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => ({
            value,
            label: getProviderMeta(value).label,
            count,
          })),
      })
    }

    return groups
  }, [domains])

  const filtered = useMemo(() => {
    const lowerSearch = search.toLowerCase()
    return domains.filter((ws) => {
      if (lowerSearch && !ws.display_name.toLowerCase().includes(lowerSearch)) return false
      if (activeFilters.role && ws.role !== activeFilters.role) return false
      if (activeFilters.provider) {
        const tenants = ws.tenants ?? []
        if (!tenants.some((t) => t.provider === activeFilters.provider)) return false
      }
      return true
    }).sort(compareWorkspaces(sort, recentIds))
  }, [domains, search, activeFilters, sort, recentIds])

  const visible = filtered.slice(0, visibleCount)
  const reconnectCount = useMemo(
    () => domains.filter(workspaceNeedsReconnect).length,
    [domains],
  )

  function handleSearchChange(value: string) {
    setSearch(value)
    setVisibleCount(PAGE_SIZE)
  }

  function handleFilterChange(group: string, value: string | null) {
    setActiveFilters((prev) => ({ ...prev, [group]: value }))
    setVisibleCount(PAGE_SIZE)
  }

  function handleSortChange(value: SortKey) {
    setSort(value)
    setVisibleCount(PAGE_SIZE)
  }

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="workspaces-title">Workspaces</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Your workspaces across connected data sources
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" asChild>
            <Link to={connectionsPath} data-testid="workspaces-connected-accounts">
              Connected Accounts
            </Link>
          </Button>
          <Button onClick={() => setShowCreate(true)} data-testid="new-workspace-btn">
            New workspace
          </Button>
        </div>
      </div>

      <AwaitingInvitesBanner />

      {!isLoading && reconnectCount > 0 && (
        <ReconnectBanner count={reconnectCount} connectionsPath={connectionsPath} />
      )}

      {isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 animate-pulse rounded-lg border bg-muted" />
          ))}
        </div>
      ) : domainsStatus === "error" ? (
        <div className="rounded-lg border border-destructive/20 p-6 text-center">
          <p className="text-sm text-destructive">Failed to load workspaces.</p>
          <button
            className="mt-2 text-sm text-muted-foreground underline hover:text-foreground"
            onClick={() => fetchDomains()}
          >
            Try again
          </button>
        </div>
      ) : domains.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="text-muted-foreground">No workspaces yet.</p>
          <Button className="mt-4" onClick={() => setShowCreate(true)}>
            Create your first workspace
          </Button>
        </div>
      ) : (
        <div className="space-y-4">
          {(filterGroups.length > 0 || domains.length > 5) && (
            <SearchFilterBar
              search={search}
              onSearchChange={handleSearchChange}
              placeholder="Search workspaces..."
              filters={filterGroups}
              activeFilters={activeFilters}
              onFilterChange={handleFilterChange}
            />
          )}

          {/* Own row: the filter bar's chips don't shrink, so sharing it would squeeze search. */}
          <div className="flex justify-end">
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              Sort
              <select
                value={sort}
                onChange={(e) => handleSortChange(e.target.value as SortKey)}
                data-testid="workspaces-sort"
                className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {SORT_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {filtered.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <p className="text-muted-foreground">No workspaces match your search.</p>
            </div>
          ) : (
            <>
              <div className="space-y-2">
                {visible.map((ws) => (
                  <WorkspaceRow
                    key={ws.id}
                    workspace={ws}
                    onClick={() => navigate(`${pathPrefix}${workspacePath(ws)}`)}
                  />
                ))}
              </div>
              {filtered.length > PAGE_SIZE && (
                <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
                  <span data-testid="workspaces-count" aria-live="polite">
                    Showing {visible.length} of {filtered.length}
                  </span>
                  {visible.length < filtered.length && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setVisibleCount((n) => n + PAGE_SIZE)}
                      data-testid="workspaces-show-more"
                    >
                      Show more
                    </Button>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {showCreate && (
        <CreateWorkspaceModal onClose={() => setShowCreate(false)} />
      )}
    </div>
  )
}
