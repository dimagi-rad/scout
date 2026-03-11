import { api } from "./client"

// ── Types ──────────────────────────────────────────────────────────────────

export interface WorkspaceDetail {
  id: string
  name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  system_prompt: string
  schema_status: "available" | "provisioning" | "unavailable"
  tenant_count: number
  member_count: number
  created_at: string
  updated_at: string
}

export interface WorkspaceMember {
  id: string       // backend returns str(m.id)
  user_id: string  // backend returns str(m.user.id)
  email: string
  name: string
  role: "read" | "read_write" | "manage"
  created_at: string
}

export interface WorkspaceTenant {
  id: string          // WorkspaceTenant UUID
  tenant_id: string   // internal Tenant UUID
  tenant_name: string
  provider: string
}

export interface UserTenant {
  id: string          // TenantMembership UUID
  provider: string
  tenant_id: string   // external ID
  tenant_uuid: string // internal Tenant UUID — use this for workspace API calls
  tenant_name: string
}

// ── Workspace CRUD ─────────────────────────────────────────────────────────

export const workspaceApi = {
  getDetail: (workspaceId: string) =>
    api.get<WorkspaceDetail>(`/api/workspaces/${workspaceId}/`),

  create: (name: string) =>
    api.post<{ id: string; name: string }>("/api/workspaces/", {
      name,
      tenant_ids: [],
    }),

  update: (workspaceId: string, body: { name?: string; system_prompt?: string }) =>
    api.patch<{ id: string; name: string }>(`/api/workspaces/${workspaceId}/`, body),

  delete: (workspaceId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/`),

  // ── Members ──────────────────────────────────────────────────────────────

  getMembers: (workspaceId: string) =>
    api.get<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members/`),

  updateMember: (workspaceId: string, membershipId: string, role: string) =>
    api.patch<{ id: string; role: string }>(
      `/api/workspaces/${workspaceId}/members/${membershipId}/`,
      { role },
    ),

  removeMember: (workspaceId: string, membershipId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/members/${membershipId}/`),

  // ── Tenants ───────────────────────────────────────────────────────────────

  getTenants: (workspaceId: string) =>
    api.get<WorkspaceTenant[]>(`/api/workspaces/${workspaceId}/tenants/`),

  addTenant: (workspaceId: string, tenantUuid: string) =>
    api.post<{ id: string; tenant_id: string; tenant_name: string }>(
      `/api/workspaces/${workspaceId}/tenants/`,
      { tenant_id: tenantUuid },
    ),

  removeTenant: (workspaceId: string, workspaceTenantId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/tenants/${workspaceTenantId}/`),

  // ── User's available tenants ──────────────────────────────────────────────

  getUserTenants: () => api.get<UserTenant[]>("/api/auth/tenants/"),
}
