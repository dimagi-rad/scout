import { useState, useMemo } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import type { TenantMembership } from "@/store/domainSlice"
import { CreateWorkspaceModal } from "@/components/CreateWorkspaceModal"
import { RoleBadge } from "@/components/RoleBadge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Users, ChevronRight } from "lucide-react"
import {
  SearchFilterBar,
  type FilterGroup,
} from "@/components/SearchFilterBar/SearchFilterBar"

const providerBadgeStyles: Record<string, string> = {
  commcare: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  "commcare-connect": "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
}

const MAX_VISIBLE_TENANTS = 4

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

function WorkspaceRow({ workspace, onClick }: { workspace: TenantMembership; onClick: () => void }) {
  const tenants = workspace.tenants ?? []

  return (
    <button
      onClick={onClick}
      data-testid={`workspace-row-${workspace.id}`}
      className="flex w-full items-center justify-between rounded-lg border bg-card px-4 py-3 text-left transition-colors hover:bg-accent"
    >
      <div className="min-w-0 flex-1">
        <div className="font-medium">{workspace.name}</div>
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
        <RoleBadge role={workspace.role} />
        <ChevronRight className="h-4 w-4 text-muted-foreground" />
      </div>
    </button>
  )
}

export function WorkspacesPage() {
  const navigate = useNavigate()
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const [showCreate, setShowCreate] = useState(false)

  // Search and filter state
  const [search, setSearch] = useState("")
  const [activeFilters, setActiveFilters] = useState<Record<string, string | null>>({
    role: null,
    provider: null,
  })

  const isLoading = domainsStatus === "loading" || domainsStatus === "idle"

  // Derive filter groups from workspace data
  const filterGroups = useMemo((): FilterGroup[] => {
    const groups: FilterGroup[] = []

    // Role filter
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

    // Provider filter
    const providerCounts = new Map<string, number>()
    for (const ws of domains) {
      const tenants = ws.tenants ?? []
      const providers = new Set(tenants.map((t) => t.provider))
      for (const p of providers) {
        providerCounts.set(p, (providerCounts.get(p) ?? 0) + 1)
      }
    }
    if (providerCounts.size > 1) {
      groups.push({
        name: "provider",
        options: [...providerCounts.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => ({ value, label: value, count })),
      })
    }

    return groups
  }, [domains])

  // Filtered workspaces
  const filtered = useMemo(() => {
    const lowerSearch = search.toLowerCase()
    return domains.filter((ws) => {
      if (lowerSearch && !ws.name.toLowerCase().includes(lowerSearch)) return false
      if (activeFilters.role && ws.role !== activeFilters.role) return false
      if (activeFilters.provider) {
        const tenants = ws.tenants ?? []
        if (!tenants.some((t) => t.provider === activeFilters.provider)) return false
      }
      return true
    })
  }, [domains, search, activeFilters])

  function handleFilterChange(group: string, value: string | null) {
    setActiveFilters((prev) => ({ ...prev, [group]: value }))
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
        <Button onClick={() => setShowCreate(true)} data-testid="new-workspace-btn">
          New workspace
        </Button>
      </div>

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
              onSearchChange={setSearch}
              placeholder="Search workspaces..."
              filters={filterGroups}
              activeFilters={activeFilters}
              onFilterChange={handleFilterChange}
            />
          )}

          {filtered.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center">
              <p className="text-muted-foreground">No workspaces match your search.</p>
            </div>
          ) : (
            <div className="space-y-2">
              {filtered.map((ws) => (
                <WorkspaceRow
                  key={ws.id}
                  workspace={ws}
                  onClick={() => navigate(`/workspaces/${ws.id}`)}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {showCreate && (
        <CreateWorkspaceModal onClose={() => setShowCreate(false)} />
      )}
    </div>
  )
}
