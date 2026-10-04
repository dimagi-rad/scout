import { useState } from "react"
import { workspaceApi } from "@/api/workspaces"
import type { WorkspaceDetail } from "@/api/workspaces"
import { ApiError } from "@/api/client"
import { useIsCurrentAccount } from "@/hooks/useIsCurrentAccount"
import { Button } from "@/components/ui/button"
import { groupMissingTenantsByRemedy, missingTenantNames } from "@/lib/missingTenants"

export function SettingsTab({
  workspace,
  onRename,
  onDelete,
}: {
  workspace: WorkspaceDetail
  onRename: (newName: string) => void
  onDelete: () => void
}) {
  const isCurrentAccount = useIsCurrentAccount()
  const [name, setName] = useState(workspace.name)
  const [savedPrompt, setSavedPrompt] = useState(workspace.system_prompt ?? "")
  const [systemPrompt, setSystemPrompt] = useState(savedPrompt)
  const [savingName, setSavingName] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)
  const [promptError, setPromptError] = useState<string | null>(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const isManager = workspace.role === "manage"
  // The server blanks the prompt and refuses every PATCH while sources are missing.
  const missingTenants = workspace.missing_tenants ?? []
  const promptRedacted = missingTenants.length > 0
  const canEdit = isManager && !promptRedacted
  const promptChanged = systemPrompt !== savedPrompt

  async function handleSaveName(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!name.trim() || name.trim() === workspace.name) return
    setSavingName(true)
    setNameError(null)
    try {
      await workspaceApi.update(workspace.id, { name: name.trim() })
      if (!isCurrentAccount()) return
      onRename(name.trim())
    } catch (err) {
      setNameError(err instanceof ApiError ? err.message : "Failed to rename workspace")
    } finally {
      setSavingName(false)
    }
  }

  async function handleSavePrompt(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault()
    if (promptRedacted || !promptChanged) return
    setSavingPrompt(true)
    setPromptError(null)
    try {
      await workspaceApi.update(workspace.id, { system_prompt: systemPrompt })
      setSavedPrompt(systemPrompt)
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
      if (!isCurrentAccount()) return
      onDelete()
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.message : "Failed to delete workspace")
      setDeleting(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-8" data-testid="settings-tab">
      {promptRedacted && (
        <div
          className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground"
          data-testid="settings-access-notice"
        >
          <p>
            {isManager
              ? "The workspace name and system prompt can't be changed, and the prompt is hidden, until you regain access."
              : "The system prompt is hidden until you regain access."}{" "}
            Still needed:
          </p>
          <ul className="mt-1 space-y-1">
            {groupMissingTenantsByRemedy(missingTenants).map(({ remedy, tenants }) => (
              <li
                key={tenants[0].tenant_id}
                data-testid={`settings-missing-${tenants[0].tenant_id}`}
              >
                <span className="font-medium text-foreground">
                  {missingTenantNames(tenants)}
                </span>
                : {remedy}
              </li>
            ))}
          </ul>
        </div>
      )}

      <section>
        <h3 className="mb-3 text-sm font-medium">Workspace name</h3>
        <form onSubmit={handleSaveName} className="flex items-start gap-3">
          <div className="flex-1">
            <input
              className="w-full rounded-md border bg-background px-3 py-2 text-sm disabled:opacity-50"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={!canEdit}
              data-testid="settings-name-input"
            />
            {nameError && <p className="mt-1 text-xs text-destructive">{nameError}</p>}
          </div>
          {canEdit && (
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
        {promptRedacted ? (
          <p
            className="text-sm text-muted-foreground"
            data-testid="settings-system-prompt-unavailable"
          >
            Hidden until you regain access.
          </p>
        ) : (
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
                disabled={savingPrompt || !promptChanged}
                data-testid="settings-save-prompt"
              >
                {savingPrompt ? "Saving…" : "Save system prompt"}
              </Button>
            )}
          </form>
        )}
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
