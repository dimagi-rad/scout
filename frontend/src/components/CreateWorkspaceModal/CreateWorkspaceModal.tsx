import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useAppStore } from "@/store/store"
import { workspaceApi } from "@/api/workspaces"
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

interface Props {
  onClose: () => void
}

export function CreateWorkspaceModal({ onClose }: Props) {
  const navigate = useNavigate()
  const fetchDomains = useAppStore((s) => s.domainActions.fetchDomains)
  const setActiveDomain = useAppStore((s) => s.domainActions.setActiveDomain)

  const [name, setName] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!name.trim()) return
    setLoading(true)
    setError(null)
    try {
      const workspace = await workspaceApi.create(name.trim())
      await fetchDomains()
      setActiveDomain(workspace.id)
      onClose()
      navigate(`/workspaces/${workspace.id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create workspace")
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="sm:max-w-md" data-testid="create-workspace-modal">
        <DialogHeader>
          <DialogTitle>New Workspace</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="py-4">
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
            {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!name.trim() || loading}
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
