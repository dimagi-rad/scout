import type { MissingTenant } from "@/api/workspaces"
import { getProviderMeta } from "@/components/WorkspaceBadge/providerMeta"

export interface MissingTenantGroup {
  remedy: string
  tenants: MissingTenant[]
}

/** Missing sources that share a remedy, in first-seen order, so each remedy is said once. */
export function groupMissingTenantsByRemedy(missing: MissingTenant[]): MissingTenantGroup[] {
  const groups = new Map<string, MissingTenant[]>()
  for (const tenant of missing) {
    const group = groups.get(tenant.remedy)
    if (group) group.push(tenant)
    else groups.set(tenant.remedy, [tenant])
  }
  return [...groups].map(([remedy, tenants]) => ({ remedy, tenants }))
}

/** "A, B, C" for a group; a blank name falls back to its provider, as the backend's text does. */
export function missingTenantNames(tenants: MissingTenant[]): string {
  return tenants.map((t) => t.tenant_name || getProviderMeta(t.provider).label).join(", ")
}
