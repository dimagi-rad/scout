import { useState, useEffect, useCallback, useRef, useMemo } from "react"
import { Link, useParams, useNavigate } from "react-router-dom"
import { workspaceApi } from "@/api/workspaces"
import {
  getUserTenantsCached,
  refreshUserTenants,
} from "@/api/userTenantsCache"
import type { WorkspaceDetail, WorkspaceMember, WorkspaceTenant, UserTenant } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { useAppStore } from "@/store/store"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ChevronLeft, Plus, RefreshCw } from "lucide-react"
import { RoleBadge } from "@/components/RoleBadge"
import { SearchFilterBar, type FilterGroup } from "@/components/SearchFilterBar/SearchFilterBar"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"

// ── Members Tab ─────────────────────────────────────────────────────────────

const DEFAULT_NEW_MEMBER_ROLE: WorkspaceMember["role"] = "read_write"

function MembersTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)

  // Add-member form state
  const [addOpen, setAddOpen] = useState(false)
  const [addEmail, setAddEmail] = useState("")
  const [addRole, setAddRole] = useState<WorkspaceMember["role"]>(DEFAULT_NEW_MEMBER_ROLE)
  const [addSubmitting, setAddSubmitting] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)

  const addTriggerRef = useRef<HTMLButtonElement>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await workspaceApi.getMembers(workspaceId)
      setMembers(data)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load members")
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  useEffect(() => { load() }, [load])

  async function handleRoleChange(membershipId: string, newRole: WorkspaceMember["role"]) {
    setUpdatingId(membershipId)
    try {
      await workspaceApi.updateMember(workspaceId, membershipId, newRole)
      setMembers((prev) =>
        prev.map((m) => (m.id === membershipId ? { ...m, role: newRole as WorkspaceMember["role"] } : m))
      )
    } catch (err) {
      setMutationError(err instanceof ApiError ? err.message : "Failed to update role")
    } finally {
      setUpdatingId(null)
    }
  }

  async function handleRemove(membershipId: string) {
    setRemovingId(membershipId)
    try {
      await workspaceApi.removeMember(workspaceId, membershipId)
      setMembers((prev) => prev.filter((m) => m.id !== membershipId))
      setConfirmRemoveId(null)
    } catch (err) {
      setMutationError(err instanceof ApiError ? err.message : "Failed to remove member")
    } finally {
      setRemovingId(null)
    }
  }

  async function handleAdd() {
    const email = addEmail.trim()
    if (!email) {
      setAddError("Email is required.")
      return
    }
    setAddSubmitting(true)
    setAddError(null)
    try {
      const newMember = await workspaceApi.addMember(workspaceId, {
        email,
        role: addRole,
      })
      setMembers((prev) => [...prev, newMember])
      setAddEmail("")
      setAddRole(DEFAULT_NEW_MEMBER_ROLE)
      setAddOpen(false)
      // Defer focus until after the trigger button is re-rendered
      setTimeout(() => addTriggerRef.current?.focus(), 0)
    } catch (err) {
      setAddError(err instanceof ApiError ? err.message : "Failed to add member")
    } finally {
      setAddSubmitting(false)
    }
  }

  function handleAddCancel() {
    setAddOpen(false)
    setAddEmail("")
    setAddRole(DEFAULT_NEW_MEMBER_ROLE)
    setAddError(null)
    setTimeout(() => addTriggerRef.current?.focus(), 0)
  }

  if (loading) return <div className="py-8 text-center text-muted-foreground">Loading…</div>
  if (error) return <div className="py-8 text-center text-destructive">{error}</div>

  return (
    <div data-testid="members-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {members.length} {members.length === 1 ? "member" : "members"}
        </span>
        {isManager && !addOpen && (
          <Button
            ref={addTriggerRef}
            size="sm"
            onClick={() => setAddOpen(true)}
            data-testid="add-member-button"
          >
            + Add member
          </Button>
        )}
      </div>

      {isManager && addOpen && (
        <form
          className="mb-4 rounded-lg border p-3"
          data-testid="add-member-form"
          onSubmit={(e) => {
            e.preventDefault()
            handleAdd()
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault()
              handleAddCancel()
            }
          }}
        >
          <div className="flex items-center gap-2">
            <Input
              type="email"
              autoFocus
              placeholder="alice@example.com"
              className="flex-1"
              aria-label="Member email"
              value={addEmail}
              onChange={(e) => setAddEmail(e.target.value)}
              disabled={addSubmitting}
              data-testid="add-member-email"
            />
            <Select
              value={addRole}
              onValueChange={(v) => setAddRole(v as WorkspaceMember["role"])}
              disabled={addSubmitting}
            >
              <SelectTrigger
                className="h-9 w-36"
                aria-label="Member role"
                data-testid="add-member-role"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="read">Read</SelectItem>
                <SelectItem value="read_write">Read-Write</SelectItem>
                <SelectItem value="manage">Manager</SelectItem>
              </SelectContent>
            </Select>
            <Button
              type="submit"
              size="sm"
              disabled={addSubmitting}
              data-testid="add-member-submit"
            >
              {addSubmitting ? "Adding…" : "Add"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={handleAddCancel}
              disabled={addSubmitting}
              data-testid="add-member-cancel"
            >
              Cancel
            </Button>
          </div>
          {addError && (
            <p className="mt-2 text-sm text-destructive" data-testid="add-member-error">
              {addError}
            </p>
          )}
        </form>
      )}

      {mutationError && (
        <p className="mb-3 text-sm text-destructive">{mutationError}</p>
      )}
      <div className="rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-muted/50">
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">User</th>
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">Role</th>
              {isManager && <th className="px-4 py-2" />}
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.id} className="border-b last:border-0" data-testid={`member-row-${member.id}`}>
                <td className="px-4 py-3">
                  <div className="font-medium">{member.name || member.email}</div>
                  <div className="text-xs text-muted-foreground">{member.email}</div>
                </td>
                <td className="px-4 py-3">
                  {isManager ? (
                    <Select
                      value={member.role}
                      onValueChange={(v) => handleRoleChange(member.id, v as WorkspaceMember["role"])}
                      disabled={updatingId === member.id}
                    >
                      <SelectTrigger className="h-8 w-32" data-testid={`member-role-${member.id}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="read">Read</SelectItem>
                        <SelectItem value="read_write">Read-Write</SelectItem>
                        <SelectItem value="manage">Manager</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : (
                    <RoleBadge role={member.role} />
                  )}
                </td>
                {isManager && (
                  <td className="px-4 py-3 text-right">
                    {confirmRemoveId === member.id ? (
                      <div className="flex items-center justify-end gap-2">
                        <span className="text-xs text-muted-foreground">Remove?</span>
                        <Button variant="ghost" size="sm" onClick={() => setConfirmRemoveId(null)}>
                          Cancel
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => handleRemove(member.id)}
                          disabled={removingId === member.id}
                          data-testid={`confirm-remove-member-${member.id}`}
                        >
                          {removingId === member.id ? "Removing…" : "Confirm"}
                        </Button>
                      </div>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:text-destructive"
                        onClick={() => setConfirmRemoveId(member.id)}
                        data-testid={`remove-member-${member.id}`}
                      >
                        Remove
                      </Button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Tenants Tab ─────────────────────────────────────────────────────────────

type AvailableStatus = "idle" | "loading" | "ready" | "error"

function TenantsTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const userId = useAppStore((s) => s.user?.id)

  // Connected sources — fast local-DB query, gates only its own section.
  const [tenants, setTenants] = useState<WorkspaceTenant[]>([])
  const [connectedLoading, setConnectedLoading] = useState(true)
  const [connectedError, setConnectedError] = useState<string | null>(null)

  // Available sources — lazily fetched (potentially slow external refresh),
  // memoized for the session via the user-tenants cache.
  const [userTenants, setUserTenants] = useState<UserTenant[]>([])
  const [availableStatus, setAvailableStatus] = useState<AvailableStatus>("idle")
  const [availableError, setAvailableError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const [addingId, setAddingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [query, setQuery] = useState("")
  const [providerFilter, setProviderFilter] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)

  // Reset search + filter when the add panel is closed manually.
  useEffect(() => {
    if (!showAdd) {
      setQuery("")
      setProviderFilter(null)
    }
  }, [showAdd])

  // Load the connected sources (fast). Never blocked on the available list.
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

  // Load the available list from the session cache. Resolves instantly after
  // the first successful fetch (the first fetch may take a moment because the
  // server refreshes from the external provider APIs).
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

  // Kick off the available-list fetch in the background after first paint so a
  // session cache is warm by the time the user opens the add panel — but the
  // connected-sources view is never blocked on it.
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

  // Tenants the user has access to that are not already in this workspace
  const inWorkspaceIds = new Set(tenants.map((t) => t.tenant_id))
  const available = userTenants.filter((t) => !inWorkspaceIds.has(t.tenant_uuid))

  // Internal-UUID → external opportunity ID, for the connected list display.
  const externalIdByUuid = new Map(userTenants.map((t) => [t.tenant_uuid, t.tenant_id]))

  // Provider filter pills, reusing the Workspaces page filter design.
  // Only shown when more than one provider is present in the available set;
  // a single-provider set renders just the search box (no "All" + one pill).
  const providerFilterGroups = useMemo((): FilterGroup[] => {
    const counts = new Map<string, number>()
    for (const t of available) {
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
  }, [available])

  const normalizedQuery = query.trim().replace(/^#/, "").toLowerCase()
  const filteredAvailable = available.filter((t) => {
    if (providerFilter && t.provider !== providerFilter) return false
    if (
      normalizedQuery &&
      !t.tenant_name.toLowerCase().includes(normalizedQuery) &&
      !t.tenant_id.toLowerCase().includes(normalizedQuery)
    ) {
      return false
    }
    return true
  })

  async function handleAdd(tenant: UserTenant) {
    setAddingId(tenant.tenant_uuid)
    setMutationError(null)
    try {
      const created = await workspaceApi.addTenant(workspaceId, tenant.tenant_uuid)
      // Optimistically reflect the new connection without a round-trip; the
      // backend returns the internal tenant UUID as `tenant_id`.
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
      setMutationError(err instanceof ApiError ? err.message : "Failed to remove data source")
    } finally {
      setRemovingId(null)
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
                <SearchFilterBar
                  search={query}
                  onSearchChange={setQuery}
                  placeholder="Search by name or opportunity ID…"
                  filters={providerFilterGroups}
                  activeFilters={{ provider: providerFilter }}
                  onFilterChange={(_group, value) => setProviderFilter(value)}
                />
              </div>
              {filteredAvailable.length === 0 ? (
                <p
                  className="rounded-md border border-dashed bg-background py-6 text-center text-sm text-muted-foreground"
                  data-testid="available-sources-empty"
                >
                  No data sources match your filters.
                </p>
              ) : (
                <div className="space-y-1.5" data-testid="available-sources-list">
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
                {isManager && tenants.length > 1 && (
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
                      onClick={() => setConfirmRemoveId(t.id)}
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
    </div>
  )
}

// ── Settings Tab ─────────────────────────────────────────────────────────────

function SettingsTab({
  workspace,
  onRename,
  onDelete,
}: {
  workspace: WorkspaceDetail
  onRename: (newName: string) => void
  onDelete: () => void
}) {
  const [name, setName] = useState(workspace.name)
  const [systemPrompt, setSystemPrompt] = useState(workspace.system_prompt ?? "")
  const [savingName, setSavingName] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)
  const [promptError, setPromptError] = useState<string | null>(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const isManager = workspace.role === "manage"

  async function handleSaveName(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!name.trim() || name.trim() === workspace.name) return
    setSavingName(true)
    setNameError(null)
    try {
      await workspaceApi.update(workspace.id, { name: name.trim() })
      onRename(name.trim())
    } catch (err) {
      setNameError(err instanceof ApiError ? err.message : "Failed to rename workspace")
    } finally {
      setSavingName(false)
    }
  }

  async function handleSavePrompt(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault()
    setSavingPrompt(true)
    setPromptError(null)
    try {
      await workspaceApi.update(workspace.id, { system_prompt: systemPrompt })
    } catch (err) {
      setPromptError(err instanceof ApiError ? err.message : "Failed to save system prompt")
    } finally {
      setSavingPrompt(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      await workspaceApi.delete(workspace.id)
      onDelete()
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.message : "Failed to delete workspace")
      setDeleting(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-8" data-testid="settings-tab">
      {/* Rename */}
      <section>
        <h3 className="mb-3 text-sm font-medium">Workspace name</h3>
        <form onSubmit={handleSaveName} className="flex items-start gap-3">
          <div className="flex-1">
            <input
              className="w-full rounded-md border bg-background px-3 py-2 text-sm disabled:opacity-50"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={!isManager}
              data-testid="settings-name-input"
            />
            {nameError && <p className="mt-1 text-xs text-destructive">{nameError}</p>}
          </div>
          {isManager && (
            <Button
              type="submit"
              size="sm"
              disabled={savingName || !name.trim() || name.trim() === workspace.name}
              data-testid="settings-save-name"
            >
              {savingName ? "Saving…" : "Save"}
            </Button>
          )}
        </form>
      </section>

      {/* System Prompt */}
      <section>
        <h3 className="mb-1 text-sm font-medium">System prompt</h3>
        <p className="mb-3 text-xs text-muted-foreground">
          Custom instructions for the AI agent in this workspace.
        </p>
        <form onSubmit={handleSavePrompt} className="space-y-2">
          <textarea
            className="w-full rounded-md border bg-background px-3 py-2 text-sm disabled:opacity-50"
            rows={6}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            disabled={!isManager}
            placeholder="Leave blank for default behavior…"
            data-testid="settings-system-prompt"
          />
          {promptError && <p className="text-xs text-destructive">{promptError}</p>}
          {isManager && (
            <Button
              type="submit"
              size="sm"
              disabled={savingPrompt}
              data-testid="settings-save-prompt"
            >
              {savingPrompt ? "Saving…" : "Save system prompt"}
            </Button>
          )}
        </form>
      </section>

      {/* Danger Zone */}
      {isManager && (
        <section className="rounded-lg border border-destructive/30 p-4">
          <h3 className="mb-1 text-sm font-medium text-destructive">Danger zone</h3>
          <p className="mb-3 text-xs text-muted-foreground">
            Permanently delete this workspace and all its threads. This cannot be undone.
          </p>
          {!showDeleteConfirm ? (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setShowDeleteConfirm(true)}
              data-testid="delete-workspace-btn"
            >
              Delete workspace
            </Button>
          ) : (
            <div className="space-y-2">
              <p className="text-xs font-medium">Are you sure? This cannot be undone.</p>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => setShowDeleteConfirm(false)}>
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={handleDelete}
                  disabled={deleting}
                  data-testid="confirm-delete-workspace-btn"
                >
                  {deleting ? "Deleting…" : "Yes, delete workspace"}
                </Button>
              </div>
              {deleteError && <p className="text-xs text-destructive">{deleteError}</p>}
            </div>
          )}
        </section>
      )}
    </div>
  )
}

// ── Page ────────────────────────────────────────────────────────────────────

export function WorkspaceDetailPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)

  function handleRename(newName: string) {
    setWorkspace((prev) => prev ? { ...prev, name: newName } : prev)
    fetchDomains()  // sync sidebar
  }

  function handleDelete() {
    fetchDomains()  // removes from sidebar
    navigate("/workspaces")
  }

  useEffect(() => {
    if (!workspaceId) return
    async function fetchWorkspace() {
      setLoading(true)
      try {
        const data = await workspaceApi.getDetail(workspaceId!)
        setWorkspace(data)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load workspace")
      } finally {
        setLoading(false)
      }
    }
    void fetchWorkspace()
  }, [workspaceId])

  if (loading) return <div className="p-8 text-center text-muted-foreground">Loading…</div>
  if (error || !workspace) return <div className="p-8 text-center text-destructive">{error ?? "Workspace not found"}</div>

  const isManager = workspace.role === "manage"

  return (
    <div className="p-6">
      {/* Header */}
      <div className="mb-6">
        <Link
          to="/workspaces"
          className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          data-testid="back-to-workspaces"
        >
          <ChevronLeft className="h-4 w-4" />
          Workspaces
        </Link>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold" data-testid="workspace-name">
            {workspace.display_name}
          </h1>
          <RoleBadge role={workspace.role} />
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="members">
        <TabsList data-testid="workspace-tabs">
          <TabsTrigger value="members" data-testid="tab-members">Members</TabsTrigger>
          <TabsTrigger value="tenants" data-testid="tab-tenants">Data sources</TabsTrigger>
          <TabsTrigger value="settings" data-testid="tab-settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="members">
          <MembersTab workspaceId={workspace.id} isManager={isManager} />
        </TabsContent>

        <TabsContent value="tenants">
          <TenantsTab workspaceId={workspace.id} isManager={isManager} />
        </TabsContent>

        <TabsContent value="settings">
          <SettingsTab workspace={workspace} onRename={handleRename} onDelete={handleDelete} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
