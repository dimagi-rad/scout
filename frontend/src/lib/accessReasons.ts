/**
 * Access-denial reason codes from apps/workspaces/access.py's access_denied_body,
 * shared so the chat notice and the thread list cannot drift apart.
 */

/** Upstream access could not be confirmed in time; resending can succeed. */
export const FRESHNESS_RETRY_REASONS: ReadonlySet<string> = new Set([
  "verification_unavailable",
  "verification_in_progress",
])

/** Only reconnecting (or regaining access upstream) fixes these; a resend fails the same way. */
export const RECONNECT_REASONS: ReadonlySet<string> = new Set([
  "credential_expired",
  "credential_missing",
  "upstream_access_lost",
  "tenant_access_lost",
])

/** A workspace with no sources (#381): only a delete resolves it. */
export const NO_SOURCES_REASON = "no_sources" as const

export const ACCESS_DENIAL_REASONS = [
  "tenant_access_lost",
  "credential_missing",
  "credential_expired",
  "upstream_access_lost",
  "verification_unavailable",
  "verification_in_progress",
  NO_SOURCES_REASON,
] as const

export type AccessDenialReason = (typeof ACCESS_DENIAL_REASONS)[number]

// A lost-access denial is also rechecked on request: once an admin restores access
// upstream, only an explicit verification can restore the archived membership.
export const RECHECKABLE_REASONS: ReadonlySet<AccessDenialReason> = new Set([
  "tenant_access_lost",
  "upstream_access_lost",
  "verification_unavailable",
  "verification_in_progress",
])
