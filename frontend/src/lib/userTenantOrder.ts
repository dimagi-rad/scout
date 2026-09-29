import type { UserTenant } from "@/api/auth"

// The server orders by last_selected_at with never-selected sources first (#357);
// every source picker lists by name instead so the same sources line up everywhere.
export function compareUserTenantsByName(a: UserTenant, b: UserTenant): number {
  return a.tenant_name.localeCompare(b.tenant_name) || a.tenant_id.localeCompare(b.tenant_id)
}
