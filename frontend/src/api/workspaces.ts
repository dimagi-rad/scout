import { api } from "./client"

export type { UserTenant } from "./auth"

export interface WorkspaceListTenant {
  id: string
  tenant_name: string
  provider: string
}

export interface MissingTenant {
  tenant_id: string
  tenant_name: string
  provider: string
  recovery: "connect_source" | "access_removed" | "reconnect" | "connect_team" | "legacy_team_unknown"
  team_slug: string
  team_name: string
  remedy: string
}

export type SchemaStatus ="available" | "provisioning" | "unavailable" | "failed"

// Workspace list item — lighter shape returned by GET /api/workspaces/
export interface WorkspaceListItem {
  id: string
  name: string
  display_name: string
  is_auto_created: boolean
  role: "read" | "read_write" | "manage"
  tenants: WorkspaceListTenant[]
  // Live upstream access. The server returns every membership (so orphaned
  // workspaces stay addressable by URL) and flags the ones the user cannot use
  // every source of. Absent on older cached payloads — treat missing as true.
  has_access?: boolean
  // The sources keeping the user out, each with a server-written remedy.
  missing_tenants?: MissingTenant[]
  member_count: number
  // Recorded tenant/view schema state; does not certify semantic query readiness.
  schema_status: SchemaStatus
  // Latest completed or partial load. A later failure does not erase this history.
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

/** Recorded load/setup state, not a current query-readiness check. */
export type WorkspaceLoadState = "loading" | "recorded" | "unavailable" | "failed" | "unknown"

/**
 * Keep explicit setup problems/progress visible alongside load history.
 * An `available` schema row can exist without an active semantic model or Cube
 * schema, and an old completed/partial load can precede a failed refresh.
 * Neither field proves current data availability or a complete workspace sync.
 * Older cached payloads without `schema_status` can still show recorded history.
 */
export function workspaceLoadState(ws: {
  schema_status?: SchemaStatus
  last_synced_at?: string | null
}): WorkspaceLoadState {
  if (ws.schema_status === "provisioning") return "loading"
  if (ws.schema_status === "unavailable") return "unavailable"
  if (ws.schema_status === "failed") return "failed"
  return workspaceHasRecordedLoad(ws) ? "recorded" : "unknown"
}

/** Whether the list payload includes a historical load time, even after failure. */
export function workspaceHasRecordedLoad(ws: { last_synced_at?: string | null }): boolean {
  return Boolean(ws.last_synced_at)
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
