/**
 * Client-side handling for the backend's transient "overloaded" signal.
 *
 * When Anthropic is momentarily overloaded mid-stream, the chat endpoint emits a
 * transient `data-chat-status` part (see apps/chat/stream.py) instead of a
 * dead-end error. We auto-retry the turn once, and only surface a message if the
 * retry also fails.
 */

/** What to do after a turn that may have hit a transient overload. */
export type OverloadAction = "retry" | "notify" | "none"

/**
 * Decide what to do once a turn finishes:
 * - no retryable error seen        -> "none"
 * - first retryable error          -> "retry"  (auto-resend once)
 * - retryable error after a retry  -> "notify" (surface a message, stop)
 */
export function decideOverloadAction(args: {
  hitRetryable: boolean
  alreadyRetried: boolean
}): OverloadAction {
  if (!args.hitRetryable) return "none"
  return args.alreadyRetried ? "notify" : "retry"
}

/** True when a useChat `onData` part is the backend's retryable-error signal. */
export function isRetryableErrorPart(part: { type?: string; data?: unknown }): boolean {
  return (
    part?.type === "data-chat-status" &&
    typeof part.data === "object" &&
    part.data !== null &&
    (part.data as { kind?: unknown }).kind === "retryable-error"
  )
}

/**
 * The backend's "busy" retryable-error (a connection limit was hit; see
 * apps/common/capacity.py) carries its own backoff hint. Returns that part's
 * `retryAfter` seconds, `null` when it has none, or `undefined` for any other part.
 */
export function busyRetryAfter(part: { type?: string; data?: unknown }): number | null | undefined {
  if (!isRetryableErrorPart(part)) return undefined
  const data = part.data as { reason?: unknown; retryAfter?: unknown }
  if (data.reason !== "busy") return undefined
  return typeof data.retryAfter === "number" ? data.retryAfter : null
}

/** After a busy turn: back off and resend a few times, then hand the user a Retry. */
export function decideBusyAction(args: { attempts: number; maxAttempts: number }): "retry" | "manual" {
  return args.attempts < args.maxAttempts ? "retry" : "manual"
}
