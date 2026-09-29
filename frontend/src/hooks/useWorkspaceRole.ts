import { ApiError, asRecord } from "@/api/client"
import type { WorkspaceRole } from "@/api/workspaces"
import { useAppStore } from "@/store/store"

export const READ_ONLY_HINT = "Read-only access — ask a workspace manager"

export const READ_ONLY_DENIAL =
  "You have read-only access to this workspace. Ask a workspace manager for read-write access."

// access_denied_body's generic copy; older views omit the trailing period.
const GENERIC_DENIAL = /^Workspace not found or access denied\.?$/

export interface WorkspaceRoleAccess {
  role: WorkspaceRole | null
  canWrite: boolean
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
  return { role, canWrite: role !== "read" }
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
  if (/role required/i.test(error.message)) return READ_ONLY_DENIAL
  const body = asRecord(error.body)
  const serverMessage = Boolean(body?.error || body?.detail)
  // Only the generic denial is ambiguous; a specific server reason (lost
  // upstream access, thread ownership, ...) is more actionable than ours.
  if (!canWrite && GENERIC_DENIAL.test(error.message)) return READ_ONLY_DENIAL
  // A non-JSON 403 (e.g. Django's CSRF failure page) carries no usable message;
  // the caller's "try again" is the right advice there.
  return serverMessage ? error.message : fallback
}

export interface ActionFailure {
  message: string
  /** False when the server says repeating the same request cannot succeed. */
  retryable: boolean
}

/**
 * Like writeErrorMessage, but also says whether offering the action again
 * makes sense. A structured 403 is final unless access_denied_body marks it
 * `retryable` (an upstream verification outage); anything else — a 5xx, a
 * dropped connection, a non-JSON 403 like a CSRF failure — may clear on its own.
 * Only access_denied_body's freshness branch sets `retryable`, so a new denial
 * reason elsewhere reads as final. Not uiSlice's RECHECKABLE_REASONS: that asks
 * whether an explicit access recheck can help, not whether resending can.
 */
export function actionFailure(
  error: unknown,
  fallback: string,
  canWrite: boolean,
): ActionFailure {
  const denial = error instanceof ApiError && error.status === 403 ? asRecord(error.body) : undefined
  return {
    message: writeErrorMessage(error, fallback, canWrite),
    retryable: !denial || denial.retryable === true,
  }
}
