import type { UserTenant } from "@/api/auth"

// Locale-aware on the client: the server's name order (#357) compares Lower() under the
// database collation, which need not match the browser's.
export function compareUserTenantsByName(a: UserTenant, b: UserTenant): number {
  return a.tenant_name.localeCompare(b.tenant_name) || a.tenant_id.localeCompare(b.tenant_id)
}
