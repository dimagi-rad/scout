import { api } from "./client"

export interface UserTenant {
  id: string          // TenantMembership UUID
  provider: string
  tenant_id: string   // external ID
  tenant_uuid: string // internal Tenant UUID — use this for workspace API calls
  tenant_name: string
  last_selected_at: string | null
  server?: string
  // Provider-specific metadata for filtering (Connect: is_active, is_test, organization, …).
  // Absent from older API responses and {} for providers that send none; treat every key as untrusted.
  attributes?: Record<string, unknown>
}

export const authApi = {
  // refresh=1 makes the server re-resolve upstream access instead of trusting its hourly TTL.
  getUserTenants: (options?: { refresh?: boolean }) =>
    api.get<UserTenant[]>(`/api/auth/tenants/${options?.refresh ? "?refresh=1" : ""}`),
}
