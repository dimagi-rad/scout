import { useState } from "react"
import { Loader2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { TenantMembership } from "@/store/domainSlice"
import { DomainPicker } from "./DomainPicker"

interface CreateWorkspaceFormProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  domains: TenantMembership[]
  onSubmit: (data: { name: string; tenant_ids: string[] }) => Promise<void>
}

export function CreateWorkspaceForm({
  open,
  onOpenChange,
  domains,
  onSubmit,
}: CreateWorkspaceFormProps) {
  const [name, setName] = useState("")
  const [selectedTenantIds, setSelectedTenantIds] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggleTenant = (tenantId: string) => {
    setSelectedTenantIds((prev) => {
      const next = new Set(prev)
      if (next.has(tenantId)) {
        next.delete(tenantId)
      } else {
        next.add(tenantId)
      }
      return next
    })
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    if (selectedTenantIds.size === 0) {
      setError("Please select at least one tenant.")
      return
    }

    setLoading(true)
    setError(null)

    try {
      await onSubmit({
        name: name.trim(),
        tenant_ids: Array.from(selectedTenantIds),
      })
      // Reset form on success
      setName("")
      setSelectedTenantIds(new Set())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workspace")
    } finally {
      setLoading(false)
    }
  }

  const handleOpenChange = (open: boolean) => {
    if (!open) {
      setName("")
      setSelectedTenantIds(new Set())
      setError(null)
    }
    onOpenChange(open)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-lg" data-testid="create-workspace-dialog">
        <DialogHeader>
          <DialogTitle>Create Custom Workspace</DialogTitle>
          <DialogDescription>
            Create a workspace that combines data from multiple tenants.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          {error && (
            <div
              className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive"
              data-testid="create-workspace-error"
            >
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="workspace-name">Name</Label>
              <Input
                id="workspace-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="My Workspace"
                required
                data-testid="create-workspace-name"
              />
            </div>

            <div className="space-y-2">
              <Label>Tenants</Label>
              <p className="text-xs text-muted-foreground">
                Select which tenants to include in this workspace.
              </p>

              <div className="rounded-md border">
                <DomainPicker
                  domains={domains}
                  mode="multi"
                  selectedIds={selectedTenantIds}
                  onToggle={toggleTenant}
                />
              </div>
            </div>
          </div>

          <DialogFooter className="mt-6">
            <Button
              type="button"
              variant="outline"
              onClick={() => handleOpenChange(false)}
              data-testid="create-workspace-cancel"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={loading || !name.trim() || selectedTenantIds.size === 0}
              data-testid="create-workspace-submit"
            >
              {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
