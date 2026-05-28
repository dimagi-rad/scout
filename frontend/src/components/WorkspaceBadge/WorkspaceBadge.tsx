// frontend/src/components/WorkspaceBadge/WorkspaceBadge.tsx
import { useAppStore } from "@/store/store"
import { getProviderMeta } from "./providerMeta"

export function WorkspaceBadge() {
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const workspace = useAppStore((s) =>
    s.domains.find((d) => d.id === s.activeDomainId),
  )

  if (!activeDomainId || !workspace) return null

  const firstProvider = workspace.tenants[0]?.provider
  const { label, Icon } = getProviderMeta(firstProvider)
  const isMultiTenant = workspace.tenants.length > 1
  const tenantNames = workspace.tenants
    .map((t) => t.tenant_name)
    .join(", ")

  return (
    <div
      className="inline-flex items-center gap-2 rounded-full border bg-background px-3 py-1.5 text-sm shadow-sm"
      data-testid="workspace-badge"
      data-provider={firstProvider ?? "unknown"}
      title={isMultiTenant ? `${label} • ${tenantNames}` : label}
    >
      <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
      <div className="flex flex-col leading-tight">
        <span className="font-medium">{workspace.display_name}</span>
        {isMultiTenant && (
          <span className="text-xs text-muted-foreground truncate max-w-[24ch]">
            {tenantNames}
          </span>
        )}
      </div>
    </div>
  )
}
