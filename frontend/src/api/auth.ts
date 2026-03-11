import { api } from "./client"

export interface UserTenant {
  id: string          // TenantMembership UUID
  provider: string
  tenant_id: string   // external ID
  tenant_uuid: string // internal Tenant UUID — use this for workspace API calls
  tenant_name: string
  last_selected_at: string | null
}

export const authApi = {
  getUserTenants: () => api.get<UserTenant[]>("/api/auth/tenants/"),
}
