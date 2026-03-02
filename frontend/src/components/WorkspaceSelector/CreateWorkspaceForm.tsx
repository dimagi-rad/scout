import { useState, useMemo } from "react"
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

  const commcareDomains = useMemo(
    () => domains.filter((d) => d.provider === "commcare"),
    [domains],
  )
  const connectDomains = useMemo(
    () => domains.filter((d) => d.provider === "commcare_connect"),
    [domains],
  )

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

              <div className="max-h-48 overflow-y-auto rounded-md border p-3 space-y-3">
                {commcareDomains.length > 0 && (
                  <TenantGroup
                    label="CommCare"
                    domains={commcareDomains}
                    selectedIds={selectedTenantIds}
                    onToggle={toggleTenant}
                  />
                )}
                {connectDomains.length > 0 && (
                  <TenantGroup
                    label="Connect"
                    domains={connectDomains}
                    selectedIds={selectedTenantIds}
                    onToggle={toggleTenant}
                  />
                )}
                {commcareDomains.length === 0 && connectDomains.length === 0 && (
                  <p className="py-2 text-center text-sm text-muted-foreground">
                    No tenants available.
                  </p>
                )}
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

function TenantGroup({
  label,
  domains,
  selectedIds,
  onToggle,
}: {
  label: string
  domains: TenantMembership[]
  selectedIds: Set<string>
  onToggle: (tenantId: string) => void
}) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-muted-foreground">{label}</p>
      <div className="space-y-1">
        {domains.map((d) => (
          <label
            key={d.id}
            className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-accent"
            data-testid={`create-workspace-tenant-${d.tenant_id}`}
          >
            <input
              type="checkbox"
              checked={selectedIds.has(d.tenant_id)}
              onChange={() => onToggle(d.tenant_id)}
              className="h-4 w-4 rounded border-input"
            />
            {d.tenant_name}
          </label>
        ))}
      </div>
    </div>
  )
}
