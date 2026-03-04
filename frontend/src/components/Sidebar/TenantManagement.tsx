import { useState } from "react"
import { X, Plus, ChevronDown, ChevronRight, Database } from "lucide-react"
import { useAppStore } from "@/store/store"
import { Button } from "@/components/ui/button"
import type { CustomWorkspaceDetail, CustomWorkspaceTenant } from "@/store/workspaceSlice"
import type { TenantMembership } from "@/store/domainSlice"

interface TenantManagementProps {
  workspace: CustomWorkspaceDetail
  domains: TenantMembership[]
  isOwner: boolean
}

export function TenantManagement({ workspace, domains, isOwner }: TenantManagementProps) {
  const [expanded, setExpanded] = useState(false)
  const [showAddMenu, setShowAddMenu] = useState(false)
  const [removing, setRemoving] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  const addTenant = useAppStore((s) => s.workspaceActions.addTenantToWorkspace)
  const removeTenant = useAppStore((s) => s.workspaceActions.removeTenantFromWorkspace)

  const currentTenantIds = new Set(workspace.tenants.map((t) => t.tenant_id))
  const availableDomains = domains.filter((d) => !currentTenantIds.has(d.tenant_id))

  const handleRemove = async (cwt: CustomWorkspaceTenant) => {
    setRemoving(cwt.id)
    try {
      await removeTenant(workspace.id, cwt.id)
    } catch {
      // error handling could be added here
    } finally {
      setRemoving(null)
    }
  }

  const handleAdd = async (domain: TenantMembership) => {
    setAdding(true)
    try {
      await addTenant(workspace.id, domain.tenant_id)
      setShowAddMenu(false)
    } catch {
      // error handling could be added here
    } finally {
      setAdding(false)
    }
  }

  return (
    <div className="border-t px-4 py-2" data-testid="tenant-management">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
        data-testid="tenant-management-toggle"
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        <Database className="h-3 w-3" />
        Tenants ({workspace.tenants.length})
      </button>

      {expanded && (
        <div className="mt-2 space-y-1">
          {workspace.tenants.map((t) => (
            <div
              key={t.id}
              className="flex items-center justify-between rounded px-2 py-1 text-xs"
              data-testid={`tenant-chip-${t.tenant_id}`}
            >
              <span className="truncate text-foreground">{t.tenant_name}</span>
              {isOwner && workspace.tenants.length > 1 && (
                <button
                  onClick={() => handleRemove(t)}
                  disabled={removing === t.id}
                  className="ml-1 shrink-0 rounded p-0.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  title={`Remove ${t.tenant_name}`}
                  data-testid={`tenant-remove-${t.tenant_id}`}
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          ))}

          {isOwner && availableDomains.length > 0 && (
            <div className="relative mt-1">
              <Button
                variant="ghost"
                size="sm"
                className="h-6 w-full justify-start px-2 text-xs"
                onClick={() => setShowAddMenu(!showAddMenu)}
                disabled={adding}
                data-testid="tenant-add-btn"
              >
                <Plus className="mr-1 h-3 w-3" />
                Add Tenant
              </Button>

              {showAddMenu && (
                <div className="absolute left-0 z-10 mt-1 w-full rounded-md border bg-popover shadow-md">
                  {availableDomains.map((d) => (
                    <button
                      key={d.id}
                      onClick={() => handleAdd(d)}
                      disabled={adding}
                      className="flex w-full items-center px-3 py-1.5 text-left text-xs hover:bg-accent disabled:opacity-50"
                      data-testid={`tenant-add-option-${d.tenant_id}`}
                    >
                      {d.tenant_name}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
