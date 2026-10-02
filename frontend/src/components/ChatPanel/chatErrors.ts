import { ApiError } from "@/api/client"
import { FRESHNESS_RETRY_REASONS, NO_SOURCES_REASON, RECONNECT_REASONS } from "@/lib/accessReasons"
import { CONNECTIONS_PATH } from "@/lib/routes"

export const ACCESS_RETRY_MESSAGE =
  "We couldn't confirm your access to this workspace's data just now. " +
  "Your conversation is safe — try again in a moment."
export const GENERIC_CHAT_ERROR_MESSAGE =
  "Something went wrong sending that message. Your conversation is saved — " +
  "retrying usually works; if it keeps failing, start a new chat."

// The upstream recheck did not finish in time (apps/workspaces/services/access_freshness.py).
// Deliberately no auto-retry, unlike busy errors: verification_unavailable usually
// means the server already spent its whole 10 s interactive budget and the aborted
// check published nothing, so a silent resend would double the wait and the load on a
// provider that is already slow. Some verification_in_progress answers come back fast
// and would pass on a resend, but the manual Retry covers those at no extra cost.
// Matched on the body only: the transport drops the status, so a 403 and a 503
// carrying the same reason classify the same way.
const ACCESS_RETRY_REASONS = FRESHNESS_RETRY_REASONS
// Resending the same message fails the same way; show the backend's remedy instead.
const FINAL_REASONS: ReadonlySet<string> = new Set([NO_SOURCES_REASON, "message_too_long"])

export type ChatErrorKind =
  | { kind: "stale" }
  | { kind: "access-retry" }
  | { kind: "access-reconnect"; message: string; recoveryPath: string }
  | { kind: "final"; message: string }
  | { kind: "generic" }

function isStaleThreadError(error: Error): boolean {
  if (error instanceof ApiError && error.status === 404) return true
  return error.message.includes("Thread not found")
}

/**
 * Only an in-app path; anything absolute or protocol-relative falls back. Browsers
 * read a backslash as a slash and strip tabs and newlines, so "/\evil.example" and
 * "/\t/evil.example" would be protocol-relative too.
 */
function safeRecoveryPath(value: unknown): string {
  if (
    typeof value === "string" &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.includes("\\") &&
    // eslint-disable-next-line no-control-regex -- rejecting control characters is the point
    !/[\u0000-\u001F\u007F]/.test(value)
  ) {
    return value
  }
  return CONNECTIONS_PATH
}

/**
 * useChat surfaces a failed chat POST as an Error whose message is the raw
 * response body. Backend text is shown only for reason codes we recognise, so an
 * arbitrary body (a proxy's HTML page, a traceback) never reaches the user.
 */
export function classifyChatError(error: Error): ChatErrorKind {
  if (isStaleThreadError(error)) return { kind: "stale" }
  let body: unknown
  try {
    body = JSON.parse(error.message)
  } catch {
    return { kind: "generic" }
  }
  if (typeof body !== "object" || body === null) return { kind: "generic" }
  const { reason, error: message, recovery_url: recoveryUrl } = body as Record<string, unknown>
  if (typeof reason !== "string") return { kind: "generic" }
  if (ACCESS_RETRY_REASONS.has(reason)) return { kind: "access-retry" }
  if (RECONNECT_REASONS.has(reason) && typeof message === "string" && message) {
    return { kind: "access-reconnect", message, recoveryPath: safeRecoveryPath(recoveryUrl) }
  }
  if (FINAL_REASONS.has(reason) && typeof message === "string" && message) {
    return { kind: "final", message }
  }
  return { kind: "generic" }
}
