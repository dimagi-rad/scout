import { useState, useEffect, useCallback } from "react"
import { Link, useParams, useNavigate } from "react-router-dom"
import { workspaceApi } from "@/api/workspaces"
import { authApi } from "@/api/auth"
import type { WorkspaceDetail, WorkspaceMember, WorkspaceTenant, UserTenant } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { useAppStore } from "@/store/store"
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
import { RoleBadge } from "@/components/RoleBadge"

// ── Members Tab ─────────────────────────────────────────────────────────────

function MembersTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updatingId, setUpdatingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)

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

  if (loading) return <div className="py-8 text-center text-muted-foreground">Loading…</div>
  if (error) return <div className="py-8 text-center text-destructive">{error}</div>

  return (
    <div data-testid="members-tab">
      <div className="mb-4 flex items-center justify-between">
        <span className="text-sm text-muted-foreground">
          {members.length} {members.length === 1 ? "member" : "members"}
        </span>
      </div>
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

function TenantsTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
  const [tenants, setTenants] = useState<WorkspaceTenant[]>([])
  const [userTenants, setUserTenants] = useState<UserTenant[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [addingId, setAddingId] = useState<string | null>(null)
  const [removingId, setRemovingId] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  const [mutationError, setMutationError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
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
      setMutationError(err instanceof ApiError ? err.message : "Failed to add data source")
    } finally {
      setAddingId(null)
    }
  }

  async function handleRemove(wt: WorkspaceTenant) {
    setRemovingId(wt.id)
    try {
      await workspaceApi.removeTenant(workspaceId, wt.id)
      await load()  // consistent with handleAdd
      setConfirmRemoveId(null)
    } catch (err) {
      setMutationError(err instanceof ApiError ? err.message : "Failed to remove data source")
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

      {mutationError && (
        <p className="mb-3 text-sm text-destructive">{mutationError}</p>
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
          ))}
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
    <div className="space-y-8" data-testid="settings-tab">
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
                  data-testid="delete-workspace-btn"
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
          <SettingsTab workspace={workspace} onRename={handleRename} onDelete={handleDelete} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
