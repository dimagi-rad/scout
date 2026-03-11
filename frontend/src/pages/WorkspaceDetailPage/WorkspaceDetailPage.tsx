import { useState, useEffect, useCallback } from "react"
import { Link, useParams } from "react-router-dom"
import { workspaceApi } from "@/api/workspaces"
import { authApi } from "@/api/auth"
import type { WorkspaceDetail, WorkspaceMember, WorkspaceTenant, UserTenant } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ChevronLeft } from "lucide-react"

// ── Role badge ──────────────────────────────────────────────────────────────

function RoleBadge({ role }: { role: string }) {
  const styles: Record<string, string> = {
    manage: "bg-green-950 text-green-400",
    read_write: "bg-blue-950 text-blue-400",
    read: "bg-muted text-muted-foreground",
  }
  const labels: Record<string, string> = {
    manage: "Manager",
    read_write: "Read-Write",
    read: "Read",
  }
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[role] ?? styles.read}`}>
      {labels[role] ?? role}
    </span>
  )
}

// ── Members Tab ─────────────────────────────────────────────────────────────

function MembersTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)

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

  async function handleRoleChange(membershipId: string, newRole: string) {
    setUpdatingId(membershipId)
    try {
      await workspaceApi.updateMember(workspaceId, membershipId, newRole)
      setMembers((prev) =>
        prev.map((m) => (m.id === membershipId ? { ...m, role: newRole as WorkspaceMember["role"] } : m))
      )
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to update role")
    } finally {
      setUpdatingId(null)
    }
  }

  async function handleRemove(membershipId: string) {
    if (!confirm("Remove this member from the workspace?")) return
    setRemovingId(membershipId)
    try {
      await workspaceApi.removeMember(workspaceId, membershipId)
      setMembers((prev) => prev.filter((m) => m.id !== membershipId))
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to remove member")
    } finally {
      setRemovingId(null)
    }
  }

  if (loading) return <div className="py-8 text-center text-muted-foreground">Loading…</div>
  if (error) return <div className="py-8 text-center text-destructive">{error}</div>

  return (
    <div data-testid="members-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {members.length} {members.length === 1 ? "member" : "members"}
        </span>
      </div>
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
                      onValueChange={(v) => handleRoleChange(member.id, v)}
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
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive"
                      onClick={() => handleRemove(member.id)}
                      disabled={removingId === member.id}
                      data-testid={`remove-member-${member.id}`}
                    >
                      Remove
                    </Button>
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

function TenantsTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [tenants, setTenants] = useState<WorkspaceTenant[]>([])
  const [userTenants, setUserTenants] = useState<UserTenant[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [addingId, setAddingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [wsTenants, allTenants] = await Promise.all([
        workspaceApi.getTenants(workspaceId),
        authApi.getUserTenants(),
      ])
      setTenants(wsTenants)
      setUserTenants(allTenants)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load tenants")
    } finally {
      setLoading(false)
    }
  }, [workspaceId])

  useEffect(() => { load() }, [load])

  // Tenants the user has access to that are not already in this workspace
  const inWorkspaceIds = new Set(tenants.map((t) => t.tenant_id))
  const available = userTenants.filter((t) => !inWorkspaceIds.has(t.tenant_uuid))

  async function handleAdd(tenantUuid: string) {
    setAddingId(tenantUuid)
    try {
      await workspaceApi.addTenant(workspaceId, tenantUuid)
      await load()
      setShowAdd(false)
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to add data source")
    } finally {
      setAddingId(null)
    }
  }

  async function handleRemove(wt: WorkspaceTenant) {
    if (!confirm(`Remove "${wt.tenant_name}" from this workspace?`)) return
    setRemovingId(wt.id)
    try {
      await workspaceApi.removeTenant(workspaceId, wt.id)
      setTenants((prev) => prev.filter((t) => t.id !== wt.id))
    } catch (err) {
      alert(err instanceof ApiError ? err.message : "Failed to remove data source")
    } finally {
      setRemovingId(null)
    }
  }

  if (loading) return <div className="py-8 text-center text-muted-foreground">Loading…</div>
  if (error) return <div className="py-8 text-center text-destructive">{error}</div>

  return (
    <div data-testid="tenants-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {tenants.length} connected {tenants.length === 1 ? "source" : "sources"}
        </span>
        {isManager && available.length > 0 && (
          <Button size="sm" variant="outline" onClick={() => setShowAdd((v) => !v)} data-testid="add-tenant-btn">
            + Add data source
          </Button>
        )}
      </div>

      {showAdd && available.length > 0 && (
        <div className="mb-4 rounded-lg border bg-muted/30 p-4">
          <p className="mb-2 text-sm font-medium">Available data sources</p>
          <div className="space-y-2">
            {available.map((t) => (
              <div key={t.tenant_uuid} className="flex items-center justify-between">
                <div>
                  <span className="text-sm">{t.tenant_name}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{t.provider}</span>
                </div>
                <Button
                  size="sm"
                  onClick={() => handleAdd(t.tenant_uuid)}
                  disabled={addingId === t.tenant_uuid}
                  data-testid={`add-tenant-${t.tenant_uuid}`}
                >
                  {addingId === t.tenant_uuid ? "Adding…" : "Add"}
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}

      {tenants.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground">
          No data sources connected.
        </div>
      ) : (
        <div className="rounded-lg border">
          {tenants.map((t, i) => (
            <div
              key={t.id}
              className={`flex items-center justify-between px-4 py-3 ${i < tenants.length - 1 ? "border-b" : ""}`}
              data-testid={`tenant-row-${t.id}`}
            >
              <div>
                <div className="font-medium">{t.tenant_name}</div>
                <div className="text-xs text-muted-foreground">{t.provider}</div>
              </div>
              {isManager && tenants.length > 1 && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  onClick={() => handleRemove(t)}
                  disabled={removingId === t.id}
                  data-testid={`remove-tenant-${t.id}`}
                >
                  Remove
                </Button>
              )}
            </div>
          ))}
        </div>
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
    <div className="mx-auto max-w-2xl px-6 py-8">
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
            {workspace.name}
          </h1>
          <RoleBadge role={workspace.role} />
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="members">
        <TabsList data-testid="workspace-tabs">
          <TabsTrigger value="members" data-testid="tab-members">Members</TabsTrigger>
          <TabsTrigger value="tenants" data-testid="tab-tenants">Tenants</TabsTrigger>
          <TabsTrigger value="settings" data-testid="tab-settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="members">
          <MembersTab workspaceId={workspace.id} isManager={isManager} />
        </TabsContent>

        <TabsContent value="tenants">
          <TenantsTab workspaceId={workspace.id} isManager={isManager} />
        </TabsContent>

        <TabsContent value="settings">
          <div className="py-8 text-center text-muted-foreground">Settings (coming soon)</div>
        </TabsContent>
      </Tabs>
    </div>
  )
}
