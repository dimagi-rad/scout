import { ApiError } from "@/api/client"
import type { WorkspaceRole } from "@/api/workspaces"
import { useAppStore } from "@/store/store"

export const READ_ONLY_HINT = "Read-only access — ask a workspace manager"

export const READ_ONLY_DENIAL =
  "You have read-only access to this workspace. Ask a workspace manager for read-write access."

export interface WorkspaceRoleAccess {
  role: WorkspaceRole | null
  canWrite: boolean
  canManage: boolean
}

/**
 * The current user's role in a workspace (defaults to the active one).
 *
 * An unknown role (workspace list not loaded yet) is treated as writable: the
 * server is the real gate, and hiding controls from managers during load would
 * be worse than a read user briefly seeing one.
 */
export function useWorkspaceRole(workspaceId?: string | null): WorkspaceRoleAccess {
  const activeDomainId = useAppStore((s) => s.activeDomainId)
  const id = workspaceId ?? activeDomainId
  const role = useAppStore((s) => s.domains.find((d) => d.id === id)?.role ?? null)
  return { role, canWrite: role !== "read", canManage: role === "manage" }
}

/**
 * Turn a failed write into UI copy. The server deliberately answers a
 * role denial with the same generic 403 body as "not a member" (see
 * tests/test_http_role_policy.py), so the known read role is what lets us say
 * why; a few endpoints do name the role requirement in their message.
 */
export function writeErrorMessage(
  error: unknown,
  fallback: string,
  canWrite: boolean,
): string {
  if (!(error instanceof ApiError) || error.status !== 403) return fallback
  if (!canWrite || /role required/i.test(error.message)) return READ_ONLY_DENIAL
  return error.message
}
