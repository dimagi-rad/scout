import { useState, useEffect, useCallback, useRef, useMemo } from "react"
import { Link, useParams, useNavigate, useLocation } from "react-router-dom"
import { workspaceApi } from "@/api/workspaces"
import {
  getUserTenantsCached,
  refreshUserTenants,
} from "@/api/userTenantsCache"
import type {
  WorkspaceDetail,
  WorkspaceMember,
  WorkspaceInvite,
  WorkspaceInviteStatus,
  WorkspaceTenant,
  UserTenant,
} from "@/api/workspaces"
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
import { slugifyWorkspaceName, workspacePath } from "@/lib/workspacePath"

const DEFAULT_NEW_MEMBER_ROLE: WorkspaceMember["role"] = "read_write"

const INVITE_STATUS_LABELS: Record<WorkspaceInviteStatus, string> = {
  pending: "Invited — awaiting sign-in",
  awaiting_access: "Awaiting data access",
  accepted: "Accepted",
  revoked: "Revoked",
  expired: "Expired",
}

function InviteStatusChip({ status }: { status: WorkspaceInviteStatus }) {
  const tone =
    status === "awaiting_access"
      ? "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400"
      : "bg-muted text-muted-foreground"
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}
      data-testid={`invite-status-${status}`}
    >
      {INVITE_STATUS_LABELS[status]}
    </span>
  )
}

function MembersTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [invites, setInvites] = useState<WorkspaceInvite[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)

  const [addOpen, setAddOpen] = useState(false)
  const [addEmail, setAddEmail] = useState("")
  const [addRole, setAddRole] = useState<WorkspaceMember["role"]>(DEFAULT_NEW_MEMBER_ROLE)
  const [addSubmitting, setAddSubmitting] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [addInfo, setAddInfo] = useState<string | null>(null)

  const addTriggerRef = useRef<HTMLButtonElement>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await workspaceApi.getMembers(workspaceId)
      setMembers(data.members)
      setInvites(data.invites)
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

  async function handleInviteRoleChange(inviteId: string, newRole: WorkspaceMember["role"]) {
    setUpdatingId(inviteId)
    try {
      await workspaceApi.updateInviteRole(workspaceId, inviteId, newRole)
      setInvites((prev) =>
        prev.map((i) => (i.id === inviteId ? { ...i, role: newRole } : i))
      )
    } catch (err) {
      setMutationError(err instanceof ApiError ? err.message : "Failed to update invite role")
    } finally {
      setUpdatingId(null)
    }
  }

  async function handleRevokeInvite(inviteId: string) {
    setRemovingId(inviteId)
    try {
      await workspaceApi.revokeInvite(workspaceId, inviteId)
      setInvites((prev) => prev.filter((i) => i.id !== inviteId))
      setConfirmRemoveId(null)
    } catch (err) {
      setMutationError(err instanceof ApiError ? err.message : "Failed to revoke invite")
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
    setAddInfo(null)
    try {
      const res = await workspaceApi.addMember(workspaceId, {
        email,
        role: addRole,
      })
      if (res.result === "member") {
        setMembers((prev) => [...prev, res])
      } else {
        const { result, ...invite } = res
        // Upsert: re-inviting an outstanding invite returns the same row.
        setInvites((prev) => [...prev.filter((i) => i.id !== invite.id), invite])
        setAddInfo(
          result === "invite_pending"
            ? `Invited ${invite.email}. They'll join automatically when they sign in to Scout.`
            : `Invited ${invite.email}. They need access to this workspace's data source; it unlocks automatically once they have it.`
        )
      }
      setAddEmail("")
      setAddRole(DEFAULT_NEW_MEMBER_ROLE)
      setAddOpen(false)
      // Defer focus until the trigger button is re-rendered.
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
          {invites.length > 0 && ` · ${invites.length} invited`}
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

      {addInfo && (
        <p
          className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-900/20 dark:text-amber-300"
          data-testid="add-member-invite-info"
        >
          {addInfo}
        </p>
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

      {invites.length > 0 && (
        <div className="mt-6" data-testid="invites-section">
          <h3 className="mb-2 text-sm font-medium text-muted-foreground">Pending invites</h3>
          <div className="rounded-lg border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Email</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Status</th>
                  <th className="px-4 py-2 text-left font-medium text-muted-foreground">Role</th>
                  {isManager && <th className="px-4 py-2" />}
                </tr>
              </thead>
              <tbody>
                {invites.map((invite) => (
                  <tr
                    key={invite.id}
                    className="border-b last:border-0"
                    data-testid={`invite-row-${invite.email}`}
                  >
                    <td className="px-4 py-3 font-medium">{invite.email}</td>
                    <td className="px-4 py-3">
                      <InviteStatusChip status={invite.status} />
                    </td>
                    <td className="px-4 py-3">
                      {isManager ? (
                        <Select
                          value={invite.role}
                          onValueChange={(v) =>
                            handleInviteRoleChange(invite.id, v as WorkspaceMember["role"])
                          }
                          disabled={updatingId === invite.id}
                        >
                          <SelectTrigger
                            className="h-8 w-32"
                            data-testid={`invite-role-${invite.email}`}
                          >
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="read">Read</SelectItem>
                            <SelectItem value="read_write">Read-Write</SelectItem>
                            <SelectItem value="manage">Manager</SelectItem>
                          </SelectContent>
                        </Select>
                      ) : (
                        <RoleBadge role={invite.role} />
                      )}
                    </td>
                    {isManager && (
                      <td className="px-4 py-3 text-right">
                        {confirmRemoveId === invite.id ? (
                          <div className="flex items-center justify-end gap-2">
                            <span className="text-xs text-muted-foreground">Revoke?</span>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => setConfirmRemoveId(null)}
                            >
                              Cancel
                            </Button>
                            <Button
                              variant="destructive"
                              size="sm"
                              onClick={() => handleRevokeInvite(invite.id)}
                              disabled={removingId === invite.id}
                              data-testid={`confirm-revoke-invite-${invite.email}`}
                            >
                              {removingId === invite.id ? "Revoking…" : "Confirm"}
                            </Button>
                          </div>
                        ) : (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive hover:text-destructive"
                            onClick={() => setConfirmRemoveId(invite.id)}
                            data-testid={`invite-revoke-${invite.email}`}
                          >
                            Revoke
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
      )}
    </div>
  )
}

type AvailableStatus = "idle" | "loading" | "ready" | "error"

function TenantsTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const userId = useAppStore((s) => s.user?.id)

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
  const [providerFilter, setProviderFilter] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)

  useEffect(() => {
    if (!showAdd) {
      setQuery("")
      setProviderFilter(null)
    }
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

  const inWorkspaceIds = new Set(tenants.map((t) => t.tenant_id))
  const available = userTenants.filter((t) => !inWorkspaceIds.has(t.tenant_uuid))

  // Internal-UUID → external opportunity ID, for the connected list display.
  const externalIdByUuid = new Map(userTenants.map((t) => [t.tenant_uuid, t.tenant_id]))

  // Only shown when >1 provider present; a single-provider set renders just the search box.
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

/** Friendly, human descriptor for a single provider on the settings header. */
const PROVIDER_DESCRIPTORS: Record<string, string> = {
  commcare_connect: "Connect opportunity",
  commcare: "CommCare project",
  ocs: "Open Chat Studio bot",
}

function providerDescriptor(provider: string): string {
  return PROVIDER_DESCRIPTORS[provider] ?? getProviderMeta(provider).label
}

/**
 * Muted icon + type descriptor shown under the workspace name. Sources the
 * provider list from the store `domains` (already loaded for the workspaces
 * list) so no extra backend field is needed. Renders nothing when the
 * workspace's providers aren't known client-side yet.
 */
function WorkspaceProviderType({ workspaceId }: { workspaceId: string }) {
  const domains = useAppStore((s) => s.domains)

  const providers = useMemo(() => {
    const ws = domains.find((d) => d.id === workspaceId)
    const tenants = ws?.tenants ?? []
    return [...new Set(tenants.map((t) => t.provider))]
  }, [domains, workspaceId])

  if (providers.length === 0) return null

  if (providers.length === 1) {
    const provider = providers[0]
    const { Icon } = getProviderMeta(provider)
    return (
      <div
        className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        data-testid="workspace-provider-type"
      >
        <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>{providerDescriptor(provider)}</span>
      </div>
    )
  }

  return (
    <div
      className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
      data-testid="workspace-provider-type"
    >
      {providers.map((provider) => {
        const { label, Icon } = getProviderMeta(provider)
        return <Icon key={provider} className="h-3.5 w-3.5 shrink-0" aria-hidden aria-label={label} />
      })}
      <span>Multiple sources</span>
    </div>
  )
}

export function WorkspaceDetailPage() {
  const { workspaceId, slug } = useParams<{ workspaceId: string; slug?: string }>()
  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)

  // Keep the top-bar switcher in sync; on hard refresh `activeDomainId` would
  // otherwise default to domains[0] and show a different workspace.
  useEffect(() => {
    if (workspaceId) setActiveDomain(workspaceId)
  }, [workspaceId, setActiveDomain])

  function handleRename(newName: string) {
    setWorkspace((prev) => prev ? { ...prev, name: newName } : prev)
    fetchDomains()
  }

  function handleDelete() {
    fetchDomains()
    navigate("/workspaces")
  }

  useEffect(() => {
    if (!workspaceId) return
    async function fetchWorkspace() {
      setLoading(true)
      // Clear prior error so an in-place reload doesn't keep rendering the stale
      // error screen — the render gate is `if (error || !workspace)`.
      setError(null)
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

  // Canonicalize the URL to `/workspaces/<slug>/<uuid>` once loaded. Resolution
  // is by UUID, so this is cosmetic. Guarded on slug actually differing so it
  // can't loop; preserves the embed prefix like the switcher does.
  useEffect(() => {
    if (!workspace || workspace.id !== workspaceId) return
    const desiredSlug = slugifyWorkspaceName(workspace.display_name)
    if (!desiredSlug || slug === desiredSlug) return
    const pathPrefix = location.pathname.startsWith("/embed") ? "/embed" : ""
    navigate(`${pathPrefix}${workspacePath(workspace)}`, { replace: true })
  }, [workspace, workspaceId, slug, location.pathname, navigate])

  if (loading) return <div className="p-8 text-center text-muted-foreground">Loading…</div>
  if (error || !workspace) return <div className="p-8 text-center text-destructive">{error ?? "Workspace not found"}</div>

  const isManager = workspace.role === "manage"

  return (
    <div className="p-6">
      <div className="mb-6">
        <Link
          to="/workspaces"
          className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          data-testid="back-to-workspaces"
        >
          <ChevronLeft className="h-4 w-4" />
          Workspaces
        </Link>
        <div className="flex items-start gap-3">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold" data-testid="workspace-name">
                {workspace.display_name}
              </h1>
              <RoleBadge role={workspace.role} />
            </div>
            <WorkspaceProviderType workspaceId={workspace.id} />
          </div>
        </div>
      </div>

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
