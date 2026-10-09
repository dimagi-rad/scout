import { useState, useEffect, useCallback, useMemo } from "react"
import { workspaceApi } from "@/api/workspaces"
import {
  getUserTenantsCached,
  refreshUserTenants,
} from "@/api/userTenantsCache"
import type { WorkspaceTenant, UserTenant } from "@/api/workspaces"
import { ApiError, asRecord } from "@/api/client"
import { useAppStore } from "@/store/store"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Plus, RefreshCw } from "lucide-react"
import { FacetFilterBar } from "@/components/FacetFilterBar/FacetFilterBar"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"
import { compareUserTenantsByName } from "@/lib/userTenantOrder"
import { useFacetedList } from "@/lib/filters/useFacetedList"
import {
  TENANT_FACETS,
  normalizeTenantSearch,
  tenantMatchesSearch,
} from "@/lib/filters/tenantFacets"
import { sourceFiltersStorageKey } from "@/lib/filters/sourceFilterStorage"

type AvailableStatus = "idle" | "loading" | "ready" | "error"

function requiresWorkspaceDelete(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    err.status === 409 &&
    asRecord(err.body)?.requires_confirmation === "delete_workspace"
  )
}

export function TenantsTab({
  workspaceId,
  isManager,
  onWorkspaceDeleted,
}: {
  workspaceId: string
  isManager: boolean
  onWorkspaceDeleted: () => void
}) {
  const userId = useAppStore((s) => s.user?.id)
  const isCurrentAccount = useIsCurrentAccount()

  // Connected sources — fast local-DB query, gates only its own section.
  const [tenants, setTenants] = useState<WorkspaceTenant[]>([])
  const [connectedLoading, setConnectedLoading] = useState(true)
  const [connectedError, setConnectedError] = useState<string | null>(null)

  // Available sources — lazily fetched (slow external refresh), session-cached.
  const [userTenants, setUserTenants] = useState<UserTenant[]>([])
  const [availableStatus, setAvailableStatus] = useState<AvailableStatus>("idle")
  const [availableError, setAvailableError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const [addingId, setAddingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [query, setQuery] = useState("")
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)
  // The workspace's last source: removing it deletes the whole workspace (#381).
  const [lastSource, setLastSource] = useState<WorkspaceTenant | null>(null)
  const [deletingWorkspace, setDeletingWorkspace] = useState(false)
  const [deleteWorkspaceError, setDeleteWorkspaceError] = useState<string | null>(null)

  // Facet selections persist across openings (see useFacetedList); the search does not.
  useEffect(() => {
    if (!showAdd) setQuery("")
  }, [showAdd])

  // Never blocked on the (slower) available list.
  const loadConnected = useCallback(async () => {
    setConnectedLoading(true)
    setConnectedError(null)
    try {
      const wsTenants = await workspaceApi.getTenants(workspaceId)
      setTenants(wsTenants)
    } catch (err) {
      setConnectedError(err instanceof ApiError ? err.message : "Failed to load data sources")
    } finally {
      setConnectedLoading(false)
    }
  }, [workspaceId])

  useEffect(() => { void loadConnected() }, [loadConnected])

  // First fetch is slow (server refreshes from external provider APIs); the
  // session cache resolves instantly thereafter.
  const loadAvailable = useCallback(async () => {
    if (!userId) return
    setAvailableStatus("loading")
    setAvailableError(null)
    try {
      const all = await getUserTenantsCached(userId)
      setUserTenants(all)
      setAvailableStatus("ready")
    } catch (err) {
      setAvailableError(err instanceof ApiError ? err.message : "Failed to load available sources")
      setAvailableStatus("error")
    }
  }, [userId])

  // Warm the session cache in the background so the add panel opens instantly.
  useEffect(() => {
    if (isManager) void loadAvailable()
  }, [isManager, loadAvailable])

  async function handleRefreshAvailable() {
    if (!userId) return
    setRefreshing(true)
    setAvailableError(null)
    try {
      const all = await refreshUserTenants(userId)
      setUserTenants(all)
      setAvailableStatus("ready")
    } catch (err) {
      setAvailableError(err instanceof ApiError ? err.message : "Failed to refresh available sources")
      setAvailableStatus("error")
    } finally {
      setRefreshing(false)
    }
  }

  // Memoized so the facet memos can hit; otherwise every keystroke re-sorts.
  const available = useMemo(() => {
    const inWorkspaceIds = new Set(tenants.map((t) => t.tenant_id))
    return userTenants
      .filter((t) => !inWorkspaceIds.has(t.tenant_uuid))
      .sort(compareUserTenantsByName)
  }, [tenants, userTenants])

  // Internal-UUID → external opportunity ID, for the connected list display.
  const externalIdByUuid = new Map(userTenants.map((t) => [t.tenant_uuid, t.tenant_id]))

  const normalizedQuery = normalizeTenantSearch(query)
  const matchesSearch = useCallback(
    (t: UserTenant) => tenantMatchesSearch(t, normalizedQuery),
    [normalizedQuery],
  )
  const facetList = useFacetedList({
    items: available,
    facets: TENANT_FACETS,
    storageKey: sourceFiltersStorageKey(userId),
    predicate: matchesSearch,
  })
  const filteredAvailable = facetList.filtered

  function clearFilters() {
    setQuery("")
    facetList.clearFacets()
  }

  async function handleAdd(tenant: UserTenant) {
    setAddingId(tenant.tenant_uuid)
    setMutationError(null)
    try {
      const created = await workspaceApi.addTenant(workspaceId, tenant.tenant_uuid)
      // Optimistic update; backend returns the internal tenant UUID as `tenant_id`.
      setTenants((prev) => [
        ...prev,
        {
          id: created.id,
          tenant_id: tenant.tenant_uuid,
          tenant_name: tenant.tenant_name,
          provider: tenant.provider,
        },
      ])
    } catch (err) {
      setMutationError(err instanceof ApiError ? err.message : "Failed to add data source")
    } finally {
      setAddingId(null)
    }
  }

  async function handleRemove(wt: WorkspaceTenant) {
    setRemovingId(wt.id)
    setMutationError(null)
    try {
      await workspaceApi.removeTenant(workspaceId, wt.id)
      setTenants((prev) => prev.filter((t) => t.id !== wt.id))
      setConfirmRemoveId(null)
    } catch (err) {
      // Another manager removed the other sources since this list loaded.
      if (requiresWorkspaceDelete(err)) {
        setConfirmRemoveId(null)
        openLastSourceDialog(wt)
      } else {
        setMutationError(err instanceof ApiError ? err.message : "Failed to remove data source")
      }
    } finally {
      setRemovingId(null)
    }
  }

  function openLastSourceDialog(wt: WorkspaceTenant) {
    setDeleteWorkspaceError(null)
    setLastSource(wt)
  }

  async function handleRemoveLastSource() {
    if (!lastSource) return
    setDeletingWorkspace(true)
    setDeleteWorkspaceError(null)
    try {
      const result = await workspaceApi.removeTenant(workspaceId, lastSource.id, {
        confirmDeleteWorkspace: true,
      })
      if (!isCurrentAccount()) {
        setLastSource(null)
        return
      }
      if (result?.workspace_deleted) {
        onWorkspaceDeleted()
        return
      }
      // A source was added meanwhile, so only this one was removed; this list
      // doesn't have the new one yet.
      setLastSource(null)
      void loadConnected()
    } catch (err) {
      if (!isCurrentAccount()) {
        setLastSource(null)
        return
      }
      setDeleteWorkspaceError(err instanceof ApiError ? err.message : "Failed to delete workspace")
    } finally {
      setDeletingWorkspace(false)
    }
  }

  const canShowAddButton = isManager && (availableStatus !== "ready" || available.length > 0)

  return (
    <div data-testid="tenants-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {connectedLoading
            ? "Loading data sources…"
            : `${tenants.length} connected ${tenants.length === 1 ? "source" : "sources"}`}
        </span>
        {canShowAddButton && (
          <Button
            size="sm"
            variant={showAdd ? "secondary" : "outline"}
            onClick={() => setShowAdd((v) => !v)}
            data-testid="add-tenant-btn"
          >
            <Plus className="mr-1 h-4 w-4" />
            Add data source
          </Button>
        )}
      </div>

      {showAdd && isManager && (
        <div className="mb-4 rounded-lg border bg-muted/30 p-4" data-testid="add-tenant-panel">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm font-medium">Available data sources</p>
            {availableStatus === "ready" && (
              <button
                type="button"
                onClick={handleRefreshAvailable}
                disabled={refreshing}
                className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
                data-testid="refresh-available-sources"
              >
                <RefreshCw className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`} />
                {refreshing ? "Refreshing…" : "Refresh"}
              </button>
            )}
          </div>

          {availableStatus === "loading" ? (
            <div data-testid="available-sources-loading">
              <p className="mb-3 text-xs text-muted-foreground">
                Fetching available sources from CommCare, Connect &amp; OCS — the first load can
                take a moment.
              </p>
              <div className="space-y-2">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="flex items-center justify-between rounded-md border bg-background px-3 py-2.5"
                  >
                    <div className="space-y-1.5">
                      <Skeleton className="h-4 w-40" />
                      <Skeleton className="h-3 w-24" />
                    </div>
                    <Skeleton className="h-8 w-16 rounded-md" />
                  </div>
                ))}
              </div>
            </div>
          ) : availableStatus === "error" ? (
            <div className="rounded-md border border-destructive/30 bg-background p-4 text-center">
              <p className="text-sm text-destructive">{availableError}</p>
              <button
                type="button"
                onClick={() => void loadAvailable()}
                className="mt-2 text-sm text-muted-foreground underline hover:text-foreground"
                data-testid="retry-available-sources"
              >
                Try again
              </button>
            </div>
          ) : available.length === 0 ? (
            <p
              className="rounded-md border border-dashed bg-background py-6 text-center text-sm text-muted-foreground"
              data-testid="available-sources-none"
            >
              All of your data sources are already connected.
            </p>
          ) : (
            <>
              <div className="mb-3">
                <FacetFilterBar
                  testIdPrefix="available-sources-filter"
                  search={query}
                  onSearchChange={setQuery}
                  searchPlaceholder="Search by name or opportunity ID…"
                  facets={facetList.facets}
                  options={facetList.options}
                  selection={facetList.selection}
                  onFacetChange={facetList.setFacet}
                  onClear={clearFilters}
                  shownCount={filteredAvailable.length}
                  totalCount={available.length}
                />
              </div>
              {filteredAvailable.length === 0 ? (
                <div
                  className="rounded-md border border-dashed bg-background py-6 text-center text-sm text-muted-foreground"
                  data-testid="available-sources-empty"
                >
                  <p>No data sources match your filters.</p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    onClick={clearFilters}
                    data-testid="available-sources-filter-empty-clear"
                  >
                    Clear filters
                  </Button>
                </div>
              ) : (
                <div
                  className="max-h-[60vh] space-y-1.5 overflow-y-auto"
                  data-testid="available-sources-list"
                >
                  {filteredAvailable.map((t) => {
                    const { label, Icon } = getProviderMeta(t.provider)
                    return (
                      <div
                        key={t.tenant_uuid}
                        className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2.5"
                      >
                        <div className="flex min-w-0 items-center gap-3">
                          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                            <Icon className="h-4 w-4" />
                          </span>
                          <div className="min-w-0">
                            <div className="truncate text-sm font-medium">{t.tenant_name}</div>
                            <div className="truncate text-xs text-muted-foreground">
                              #{t.tenant_id} · {label}
                            </div>
                          </div>
                        </div>
                        <Button
                          size="sm"
                          onClick={() => handleAdd(t)}
                          disabled={addingId === t.tenant_uuid}
                          data-testid={`add-tenant-${t.tenant_uuid}`}
                        >
                          {addingId === t.tenant_uuid ? "Adding…" : "Add"}
                        </Button>
                      </div>
                    )
                  })}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {mutationError && (
        <p className="mb-3 text-sm text-destructive">{mutationError}</p>
      )}

      {connectedLoading ? (
        <div className="rounded-lg border" data-testid="connected-sources-loading">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className={`flex items-center justify-between px-4 py-3 ${i < 2 ? "border-b" : ""}`}
            >
              <div className="space-y-1.5">
                <Skeleton className="h-4 w-44" />
                <Skeleton className="h-3 w-28" />
              </div>
            </div>
          ))}
        </div>
      ) : connectedError ? (
        <div className="rounded-lg border border-destructive/30 p-6 text-center">
          <p className="text-sm text-destructive">{connectedError}</p>
          <button
            type="button"
            onClick={() => void loadConnected()}
            className="mt-2 text-sm text-muted-foreground underline hover:text-foreground"
            data-testid="retry-connected-sources"
          >
            Try again
          </button>
        </div>
      ) : tenants.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center" data-testid="connected-sources-empty">
          <p className="text-sm text-muted-foreground">No data sources connected yet.</p>
          {isManager && (
            <Button
              size="sm"
              variant="outline"
              className="mt-4"
              onClick={() => setShowAdd(true)}
              data-testid="add-tenant-empty-btn"
            >
              <Plus className="mr-1 h-4 w-4" />
              Add data source
            </Button>
          )}
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          {tenants.map((t, i) => {
            const { label, Icon } = getProviderMeta(t.provider)
            const externalId = externalIdByUuid.get(t.tenant_id)
            return (
              <div
                key={t.id}
                className={`flex items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-muted/40 ${i < tenants.length - 1 ? "border-b" : ""}`}
                data-testid={`tenant-row-${t.id}`}
              >
                <div className="flex min-w-0 items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                    <Icon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0">
                    <div className="truncate font-medium">{t.tenant_name}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {externalId ? `#${externalId} · ${label}` : label}
                    </div>
                  </div>
                </div>
                {isManager && (
                  confirmRemoveId === t.id ? (
                    <div className="flex items-center justify-end gap-2">
                      <span className="text-xs text-muted-foreground">Remove?</span>
                      <Button variant="ghost" size="sm" onClick={() => setConfirmRemoveId(null)}>
                        Cancel
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => handleRemove(t)}
                        disabled={removingId === t.id}
                        data-testid={`confirm-remove-tenant-${t.id}`}
                      >
                        {removingId === t.id ? "Removing…" : "Confirm"}
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive"
                      onClick={() =>
                        tenants.length === 1 ? openLastSourceDialog(t) : setConfirmRemoveId(t.id)
                      }
                      data-testid={`remove-tenant-${t.id}`}
                    >
                      Remove
                    </Button>
                  )
                )}
              </div>
            )
          })}
        </div>
      )}

      <AlertDialog
        open={lastSource !== null}
        onOpenChange={(open) => { if (!open && !deletingWorkspace) setLastSource(null) }}
      >
        <AlertDialogContent data-testid="last-source-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this workspace?</AlertDialogTitle>
            <AlertDialogDescription>
              {lastSource?.tenant_name ?? "This source"} is this workspace&rsquo;s only data
              source. Removing it deletes the workspace, its conversations and its data for
              every member. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteWorkspaceError && (
            <p className="text-sm text-destructive" role="alert" data-testid="last-source-error">
              {deleteWorkspaceError}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deletingWorkspace} data-testid="last-source-cancel">
              Cancel
            </AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={handleRemoveLastSource}
              disabled={deletingWorkspace}
              data-testid="last-source-confirm"
            >
              {deletingWorkspace ? "Deleting…" : "Delete workspace"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
