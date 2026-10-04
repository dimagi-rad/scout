import { useState, useEffect, useCallback, useRef } from "react"
import { workspaceApi } from "@/api/workspaces"
import type { WorkspaceMember, WorkspaceInvite, WorkspaceInviteStatus } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { RoleBadge } from "@/components/RoleBadge"

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

export function MembersTab({ workspaceId, isManager }: { workspaceId: string; isManager: boolean }) {
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
  const [addedInfo, setAddedInfo] = useState<string | null>(null)

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

  // A 409 means the list is stale: the invite was accepted or revoked elsewhere, or the
  // invitee already joined. Refresh without load()'s loading/error states, which would
  // replace the tab and hide the conflict message.
  async function refreshOnConflict(err: unknown) {
    if (!(err instanceof ApiError && err.status === 409)) return
    try {
      const data = await workspaceApi.getMembers(workspaceId)
      setMembers(data.members)
      setInvites(data.invites)
    } catch {
      // The stale list plus the conflict message beats replacing the tab with an error.
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
      void refreshOnConflict(err)
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
      void refreshOnConflict(err)
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
    setAddedInfo(null)
    try {
      const res = await workspaceApi.addMember(workspaceId, {
        email,
        role: addRole,
      })
      if (res.result === "member") {
        setMembers((prev) => [...prev, res])
        setAddedInfo(`Added ${res.email}. We've emailed them to let them know.`)
      } else {
        const awaiting =
          res.result === "invite_awaiting_access"
            ? res.needs_sign_in
              ? "Their saved sign-in has expired or can't be used; it unlocks once they sign in to Scout again and have access to this workspace's data source."
              : res.recheck_complete
                ? "They need access to this workspace's data source; it unlocks automatically once they have it."
                : "Scout couldn't finish checking their access upstream, so adding them again may help; otherwise it unlocks automatically once they have access."
            : null
        const { result, ...invite } = res
        // Upsert: re-inviting an outstanding invite returns the same row.
        setInvites((prev) => [...prev.filter((i) => i.id !== invite.id), invite])
        setAddInfo(
          result === "invite_pending"
            ? `Invited ${invite.email}. They'll join automatically when they sign in to Scout.`
            : `Invited ${invite.email}. ${awaiting}`
        )
      }
      setAddEmail("")
      setAddRole(DEFAULT_NEW_MEMBER_ROLE)
      setAddOpen(false)
      // Defer focus until the trigger button is re-rendered.
      setTimeout(() => addTriggerRef.current?.focus(), 0)
    } catch (err) {
      setAddError(err instanceof ApiError ? err.message : "Failed to add member")
      void refreshOnConflict(err)
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

      {addedInfo && (
        <p
          role="status"
          className="mb-3 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-900/40 dark:bg-emerald-900/20 dark:text-emerald-300"
          data-testid="add-member-added-info"
        >
          {addedInfo}
        </p>
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
        <p className="mb-3 text-sm text-destructive" data-testid="members-mutation-error">
          {mutationError}
        </p>
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
