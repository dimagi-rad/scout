import { api } from "./client"

export type { UserTenant } from "./auth"

// ── Types ──────────────────────────────────────────────────────────────────

// Workspace list item — lighter shape returned by GET /api/workspaces/
export interface WorkspaceListItem {
  id: string
  name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  tenant_count: number
  member_count: number
  created_at: string
}

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

// ── Workspace CRUD ─────────────────────────────────────────────────────────

export const workspaceApi = {
  list: () => api.get<WorkspaceListItem[]>("/api/workspaces/"),

  getDetail: (workspaceId: string) =>
    api.get<WorkspaceDetail>(`/api/workspaces/${workspaceId}/`),

  create: (name: string, tenantIds: string[] = []) =>
    api.post<{ id: string; name: string }>("/api/workspaces/", {
      name,
      tenant_ids: tenantIds,
    }),

  update: (workspaceId: string, body: { name?: string; system_prompt?: string }) =>
    api.patch<{ id: string; name: string }>(`/api/workspaces/${workspaceId}/`, body),

  delete: (workspaceId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/`),

  // ── Members ──────────────────────────────────────────────────────────────

  getMembers: (workspaceId: string) =>
    api.get<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members/`),

  updateMember: (workspaceId: string, membershipId: string, role: WorkspaceMember["role"]) =>
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
}
