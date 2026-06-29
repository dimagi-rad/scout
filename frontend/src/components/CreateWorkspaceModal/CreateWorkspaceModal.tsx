import { useState, useEffect, useMemo } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { workspaceApi } from "@/api/workspaces"
import { type UserTenant } from "@/api/auth"
import { getUserTenantsCached } from "@/api/userTenantsCache"
import { ApiError } from "@/api/client"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { AlertTriangle, Check } from "lucide-react"
import {
  SearchFilterBar,
  type FilterGroup,
} from "@/components/SearchFilterBar/SearchFilterBar"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { workspacePath } from "@/lib/workspacePath"

interface Props {
  onClose: () => void
}

export function CreateWorkspaceModal({ onClose }: Props) {
  const navigate = useNavigate()
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const userId = useAppStore((s) => s.user?.id)
  const domains = useAppStore((s) => s.domains)
  const domainsStatus = useAppStore((s) => s.domainsStatus)

  const [name, setName] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [sources, setSources] = useState<UserTenant[]>([])
  const [sourcesLoading, setSourcesLoading] = useState(true)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState("")
  const [providerFilter, setProviderFilter] = useState<string | null>(null)
  // Set once the user opts to "Create anyway" past the duplicate warning.
  const [duplicateAcknowledged, setDuplicateAcknowledged] = useState(false)

  useEffect(() => {
    if (!userId) return
    let cancelled = false
    async function loadSources() {
      setSourcesLoading(true)
      try {
        const data = await getUserTenantsCached(userId!)
        if (!cancelled) setSources(data)
      } catch {
        // Non-fatal: workspace can still be created without a data source.
        if (!cancelled) setSources([])
      } finally {
        if (!cancelled) setSourcesLoading(false)
      }
    }
    void loadSources()
    return () => {
      cancelled = true
    }
  }, [userId])

  // Ensure the user's workspace list is loaded so duplicate detection has data
  // to compare against, even if the modal is opened before the list is fetched.
  useEffect(() => {
    if (domainsStatus === "idle") void fetchDomains()
  }, [domainsStatus, fetchDomains])

  const providerFilterGroups = useMemo((): FilterGroup[] => {
    const counts = new Map<string, number>()
    for (const t of sources) {
      counts.set(t.provider, (counts.get(t.provider) ?? 0) + 1)
    }
    if (counts.size <= 1) return []
    return [
      {
        name: "provider",
        options: [...counts.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => ({
            value,
            label: getProviderMeta(value).label,
            count,
          })),
      },
    ]
  }, [sources])

  // A workspace is an exact duplicate when its set of tenant ids matches the
  // currently selected set, order-independent. Only meaningful for a non-empty
  // selection — an empty pick shouldn't flag every empty workspace. Detection
  // is purely client-side: the workspace list already carries each tenant's id.
  const duplicateWorkspace = useMemo(() => {
    if (selected.size === 0) return null
    return (
      domains.find((ws) => {
        const ids = ws.tenants?.map((t) => t.id) ?? []
        if (ids.length !== selected.size) return false
        return ids.every((id) => selected.has(id))
      }) ?? null
    )
  }, [domains, selected])

  const normalizedSearch = search.trim().replace(/^#/, "").toLowerCase()
  const filteredSources = sources.filter((t) => {
    if (providerFilter && t.provider !== providerFilter) return false
    if (
      normalizedSearch &&
      !t.tenant_name.toLowerCase().includes(normalizedSearch) &&
      !t.tenant_id.toLowerCase().includes(normalizedSearch)
    ) {
      return false
    }
    return true
  })

  function toggleSource(uuid: string) {
    // Changing the selection invalidates a prior "create anyway" decision: the
    // new set may match a different existing workspace (or none).
    setDuplicateAcknowledged(false)
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(uuid)) next.delete(uuid)
      else next.add(uuid)
      return next
    })
  }

  function goToExistingWorkspace() {
    if (!duplicateWorkspace) return
    setActiveDomain(duplicateWorkspace.id)
    onClose()
    navigate(workspacePath(duplicateWorkspace))
  }

  async function handleSubmit(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!name.trim()) return
    // Hold for an explicit decision when this exact data-source set already
    // exists, unless the user has chosen to create anyway.
    if (duplicateWorkspace && !duplicateAcknowledged) return
    setLoading(true)
    setError(null)
    try {
      const workspace = await workspaceApi.create(name.trim(), [...selected])
      await fetchDomains()
      setActiveDomain(workspace.id)
      onClose()
      navigate(workspacePath(workspace))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create workspace")
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-lg" data-testid="create-workspace-modal">
        <DialogHeader>
          <DialogTitle>New Workspace</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="min-w-0">
          <div className="min-w-0 space-y-4 py-4">
            <div>
              <Label htmlFor="workspace-name">Name</Label>
              <Input
                id="workspace-name"
                data-testid="workspace-name-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Acme Corp"
                className="mt-1"
                autoFocus
              />
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <Label>Data sources</Label>
                <span className="text-xs text-muted-foreground">
                  {selected.size > 0
                    ? `${selected.size} selected`
                    : "Optional"}
                </span>
              </div>
              <p className="mb-2 text-xs text-muted-foreground">
                Add at least one data source so your workspace isn&rsquo;t empty.
              </p>

              {sourcesLoading ? (
                <p className="py-4 text-center text-sm text-muted-foreground">
                  Loading data sources…
                </p>
              ) : sources.length === 0 ? (
                <p
                  className="rounded-md border border-dashed py-4 text-center text-sm text-muted-foreground"
                  data-testid="create-no-sources"
                >
                  No data sources available to add.
                </p>
              ) : (
                <div className="space-y-3">
                  <SearchFilterBar
                    search={search}
                    onSearchChange={setSearch}
                    placeholder="Search by name or opportunity ID…"
                    filters={providerFilterGroups}
                    activeFilters={{ provider: providerFilter }}
                    onFilterChange={(_group, value) => setProviderFilter(value)}
                    orientation="stacked"
                  />
                  <div
                    className="max-h-56 space-y-1 overflow-y-auto rounded-md border p-1"
                    data-testid="create-sources-list"
                  >
                    {filteredSources.length === 0 ? (
                      <p className="py-3 text-center text-sm text-muted-foreground">
                        No data sources match your filters.
                      </p>
                    ) : (
                      filteredSources.map((t) => {
                        const isSelected = selected.has(t.tenant_uuid)
                        return (
                          <button
                            type="button"
                            key={t.tenant_uuid}
                            onClick={() => toggleSource(t.tenant_uuid)}
                            aria-pressed={isSelected}
                            data-testid={`create-source-${t.tenant_uuid}`}
                            className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left transition-colors ${
                              isSelected ? "bg-accent" : "hover:bg-accent/50"
                            }`}
                          >
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium">{t.tenant_name}</div>
                              <div className="truncate text-xs text-muted-foreground">
                                #{t.tenant_id} · {getProviderMeta(t.provider).label}
                              </div>
                            </div>
                            <span
                              className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border ${
                                isSelected
                                  ? "border-primary bg-primary text-primary-foreground"
                                  : "border-muted-foreground/30"
                              }`}
                            >
                              {isSelected && <Check className="h-3 w-3" />}
                            </span>
                          </button>
                        )
                      })
                    )}
                  </div>
                </div>
              )}
            </div>

            {duplicateWorkspace && !duplicateAcknowledged && (
              <div
                className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-400"
                data-testid="create-workspace-duplicate-warning"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="min-w-0 space-y-2">
                  <p>
                    You already have a workspace with this exact set of data
                    sources:{" "}
                    <span className="font-medium">
                      {duplicateWorkspace.display_name || duplicateWorkspace.name}
                    </span>
                    .
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={goToExistingWorkspace}
                      data-testid="create-workspace-duplicate-goto"
                    >
                      Go to that workspace
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => setDuplicateAcknowledged(true)}
                      data-testid="create-workspace-duplicate-continue"
                    >
                      Create anyway
                    </Button>
                  </div>
                </div>
              </div>
            )}

            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                !name.trim() ||
                loading ||
                (!!duplicateWorkspace && !duplicateAcknowledged)
              }
              data-testid="create-workspace-submit"
            >
              {loading ? "Creating…" : "Create Workspace"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
