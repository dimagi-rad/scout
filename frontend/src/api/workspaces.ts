import { api } from "./client"

export type { UserTenant } from "./auth"

export interface WorkspaceListTenant {
  id: string
  tenant_name: string
  provider: string
}

export type SchemaStatus = "available" | "provisioning" | "unavailable" | "failed"

// Workspace list item — lighter shape returned by GET /api/workspaces/
export interface WorkspaceListItem {
  id: string
  name: string
  display_name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  tenants: WorkspaceListTenant[]
  // Live upstream access. The server returns every membership (so orphaned
  // workspaces stay addressable by URL) and flags the ones the user has lost
  // tenant access to. Absent on older cached payloads — treat missing as true.
  has_access?: boolean
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

export type WorkspaceRole = "read" | "read_write" | "manage"

export interface WorkspaceMember {
  id: string       // backend returns str(m.id)
  user_id: string  // backend returns str(m.user.id)
  email: string
  name: string
  role: WorkspaceRole
  created_at: string
}

export type WorkspaceInviteStatus =
  | "pending"
  | "awaiting_access"
  | "accepted"
  | "revoked"
  | "expired"

export interface WorkspaceInvite {
  id: string
  email: string
  role: WorkspaceRole
  status: WorkspaceInviteStatus
  created_at: string
}

export interface MembersResponse {
  members: WorkspaceMember[]
  invites: WorkspaceInvite[]
}

// POST /members/ resolves to a real member OR a pending/awaiting invite; the
// `result` discriminator tells the UI which row/message to render.
export type AddMemberResult =
  | ({ result: "member" } & WorkspaceMember)
  | ({ result: "invite_pending" | "invite_awaiting_access" } & WorkspaceInvite)

// GET /api/invites/ — the signed-in user's own awaiting_access invites.
export interface AwaitingInvite {
  id: string
  workspace_name: string
  message: string
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
 * - "empty"   — schema is `unavailable` or `failed`: no queryable data. A
 *   `failed` multi-tenant view schema means the per-tenant data loaded but the
 *   workspace's combined query layer could not be built — there is still
 *   nothing queryable, so it is treated as "empty" rather than "ready".
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
  if (ws.schema_status === "failed") return "empty"
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

/**
 * Whether the user still has live upstream access to a workspace. The server
 * omits `has_access` on older cached payloads; treat missing as accessible so a
 * stale payload never locks the whole app behind the lost-access modal.
 */
export function workspaceHasAccess(ws: { has_access?: boolean }): boolean {
  return ws.has_access !== false
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

  getMembers: (workspaceId: string) =>
    api.get<MembersResponse>(`/api/workspaces/${workspaceId}/members/`),

  addMember: (
    workspaceId: string,
    body: { email: string; role: WorkspaceRole },
  ) =>
    api.post<AddMemberResult>(
      `/api/workspaces/${workspaceId}/members/`,
      body,
    ),

  updateMember: (workspaceId: string, membershipId: string, role: WorkspaceRole) =>
    api.patch<{ id: string; role: string }>(
      `/api/workspaces/${workspaceId}/members/${membershipId}/`,
      { role },
    ),

  removeMember: (workspaceId: string, membershipId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/members/${membershipId}/`),

  updateInviteRole: (workspaceId: string, inviteId: string, role: WorkspaceRole) =>
    api.patch<WorkspaceInvite>(
      `/api/workspaces/${workspaceId}/invites/${inviteId}/`,
      { role },
    ),

  revokeInvite: (workspaceId: string, inviteId: string) =>
    api.delete<void>(`/api/workspaces/${workspaceId}/invites/${inviteId}/`),

  getMyInvites: () => api.get<AwaitingInvite[]>("/api/invites/"),

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
