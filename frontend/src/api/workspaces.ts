import { api } from "./client"

export type { UserTenant } from "./auth"

// ── Types ──────────────────────────────────────────────────────────────────

export interface WorkspaceListTenant {
  id: string
  tenant_name: string
  provider: string
}

export type SchemaStatus = "available" | "provisioning" | "unavailable"

// Workspace list item — lighter shape returned by GET /api/workspaces/
export interface WorkspaceListItem {
  id: string
  name: string
  display_name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  tenants: WorkspaceListTenant[]
  member_count: number
  schema_status: SchemaStatus
  last_synced_at: string | null
  created_at: string
}

export interface WorkspaceDetail {
  id: string
  name: string
  display_name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  system_prompt: string
  schema_status: SchemaStatus
  tenant_count: number
  member_count: number
  last_synced_at: string | null
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

/** Live data-availability state for a workspace's indicator. */
export type WorkspaceDataState = "loading" | "ready" | "empty"

/**
 * Live data-availability state, derived from the backend's `schema_status`:
 *
 * - "ready"   — schema is `available`: the workspace currently has queryable data.
 * - "loading" — schema is `provisioning`/`materializing`: data is being set up.
 * - "empty"   — schema is `unavailable`: never synced, expired, or torn down.
 *
 * Unlike `last_synced_at` (a *historical* "was synced at least once" signal),
 * `schema_status` reflects the live schema and correctly returns to "empty"
 * when a workspace's data is torn down. We fall back to `last_synced_at` only
 * when `schema_status` is absent (e.g. an older cached payload), so the UI
 * degrades safely rather than showing nothing.
 */
export function workspaceDataState(ws: {
  schema_status?: SchemaStatus
  last_synced_at?: string | null
}): WorkspaceDataState {
  if (ws.schema_status === "available") return "ready"
  if (ws.schema_status === "provisioning") return "loading"
  if (ws.schema_status === "unavailable") return "empty"
  // No live signal available — fall back to the historical sync marker.
  return ws.last_synced_at != null ? "ready" : "empty"
}

/**
 * Whether a workspace currently has queryable data. Prefers the live
 * `schema_status`; treats only the "ready" state as having data.
 * Single source of truth for the UI's has-data filter.
 */
export function workspaceHasData(ws: {
  schema_status?: SchemaStatus
  last_synced_at?: string | null
}): boolean {
  return workspaceDataState(ws) === "ready"
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
    api.patch<{ id: string; name: string; display_name: string }>(
      `/api/workspaces/${workspaceId}/`,
      body,
    ),

  delete: (workspaceId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/`),

  // ── Members ──────────────────────────────────────────────────────────────

  getMembers: (workspaceId: string) =>
    api.get<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members/`),

  addMember: (
    workspaceId: string,
    body: { email: string; role: WorkspaceMember["role"] },
  ) =>
    api.post<WorkspaceMember>(
      `/api/workspaces/${workspaceId}/members/`,
      body,
    ),

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
