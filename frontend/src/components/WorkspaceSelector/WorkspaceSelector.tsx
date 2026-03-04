import { useEffect, useState } from "react"
import { X, Plus, Users, Database, AlertTriangle } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import { CreateWorkspaceForm } from "./CreateWorkspaceForm"
import { DomainPicker } from "./DomainPicker"

interface WorkspaceSelectorProps {
  open: boolean
  onClose: () => void
}

export function WorkspaceSelector({ open, onClose }: WorkspaceSelectorProps) {
  if (!open) return null
  return <WorkspaceSelectorPanel onClose={onClose} />
}

function WorkspaceSelectorPanel({ onClose }: { onClose: () => void }) {
  const [createOpen, setCreateOpen] = useState(false)

  const domains = useAppStore((s) => s.domains)
  const customWorkspaces = useAppStore((s) => s.customWorkspaces)
  const enterError = useAppStore((s) => s.enterError)
  const missingTenants = useAppStore((s) => s.missingTenants)
  const fetchCustomWorkspaces = useAppStore((s) => s.workspaceActions.fetchCustomWorkspaces)
  const enterCustomWorkspace = useAppStore((s) => s.workspaceActions.enterCustomWorkspace)
  const createCustomWorkspace = useAppStore((s) => s.workspaceActions.createCustomWorkspace)
  const exitCustomWorkspace = useAppStore((s) => s.workspaceActions.exitCustomWorkspace)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)
  const newThread = useAppStore((s) => s.uiActions.newThread)

  // Fetch custom workspaces on mount
  useEffect(() => {
    fetchCustomWorkspaces()
  }, [fetchCustomWorkspaces])

  const handleSelectDomain = (id: string) => {
    exitCustomWorkspace()
    setActiveDomain(id)
    newThread()
    onClose()
  }

  const handleEnterWorkspace = async (id: string) => {
    await enterCustomWorkspace(id)
    // Only close if there was no error (enterError will be set by the action if failed)
    const currentError = useAppStore.getState().enterError
    if (!currentError) {
      newThread()
      onClose()
    }
  }

  const handleCreateWorkspace = async (data: { name: string; tenant_ids: string[] }) => {
    const created = await createCustomWorkspace({
      name: data.name,
      tenant_ids: data.tenant_ids,
    })
    setCreateOpen(false)
    await enterCustomWorkspace(created.id)
    const currentError = useAppStore.getState().enterError
    if (!currentError) {
      newThread()
      onClose()
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      data-testid="workspace-selector-panel"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="relative w-full max-w-2xl rounded-lg border bg-background shadow-lg">
        {/* Header */}
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold">Select Workspace</h2>
          <button
            onClick={onClose}
            className="rounded-sm opacity-70 transition-opacity hover:opacity-100"
            data-testid="workspace-selector-close"
          >
            <X className="h-4 w-4" />
            <span className="sr-only">Close</span>
          </button>
        </div>

        {/* Error Banner */}
        {enterError && (
          <div
            className="mx-6 mt-4 flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
            data-testid="workspace-enter-error"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <p className="font-medium">{enterError}</p>
              {missingTenants.length > 0 && (
                <ul className="mt-1 list-disc pl-4 text-xs">
                  {missingTenants.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}

        <DomainPicker
          domains={domains}
          mode="single"
          onSelect={handleSelectDomain}
          customTab={{
            label: "Custom",
            count: customWorkspaces.length,
            content: (
              <CustomTabContent
                workspaces={customWorkspaces}
                onEnter={handleEnterWorkspace}
                onCreate={() => setCreateOpen(true)}
              />
            ),
          }}
        />

        <CreateWorkspaceForm
          open={createOpen}
          onOpenChange={setCreateOpen}
          domains={domains}
          onSubmit={handleCreateWorkspace}
        />
      </div>
    </div>
  )
}

function CustomTabContent({
  workspaces,
  onEnter,
  onCreate,
}: {
  workspaces: { id: string; name: string; tenant_count: number; member_count: number }[]
  onEnter: (id: string) => void
  onCreate: () => void
}) {
  return (
    <div className="space-y-2">
      {workspaces.length === 0 && (
        <p className="py-4 text-center text-sm text-muted-foreground">
          No custom workspaces found.
        </p>
      )}
      {workspaces.map((w) => (
        <button
          key={w.id}
          onClick={() => onEnter(w.id)}
          data-testid={`workspace-item-${w.id}`}
          className="flex w-full items-center justify-between rounded-md border px-4 py-3 text-left transition-colors hover:bg-accent"
        >
          <div>
            <p className="text-sm font-medium">{w.name}</p>
            <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <Database className="h-3 w-3" />
                {w.tenant_count} {w.tenant_count === 1 ? "tenant" : "tenants"}
              </span>
              <span className="flex items-center gap-1">
                <Users className="h-3 w-3" />
                {w.member_count} {w.member_count === 1 ? "member" : "members"}
              </span>
            </div>
          </div>
        </button>
      ))}
      <Button
        variant="outline"
        className="mt-2 w-full"
        data-testid="workspace-create-btn"
        onClick={onCreate}
      >
        <Plus className="mr-2 h-4 w-4" />
        Create Custom Workspace
      </Button>
    </div>
  )
}
