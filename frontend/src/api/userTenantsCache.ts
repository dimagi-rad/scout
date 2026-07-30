import { authApi, type UserTenant } from "./auth"

// GET /api/auth/tenants/ synchronously refreshes the user's opportunities,
// domains, and chatbots from the live CommCare / Connect / OCS APIs whenever the
// server-side TTL cache is cold. That external round-trip can take ~20s on the
// first call of a session. We never want to pay that cost more than once, so we
// memoize the in-flight promise per user for the lifetime of the page session.
//
// The cache is keyed by user id so that switching accounts (logout/login within
// the same tab) does not surface a stale list.

let cachedUserId: string | null = null
let cachedPromise: Promise<UserTenant[]> | null = null

/**
 * Returns the user's tenant memberships, memoizing the (potentially slow) fetch
 * for the rest of the session. Subsequent calls resolve instantly. A failed
 * fetch is not cached, so the next call retries.
 */
export function getUserTenantsCached(userId: string): Promise<UserTenant[]> {
  if (cachedUserId !== userId || !cachedPromise) {
    cachedUserId = userId
    cachedPromise = authApi.getUserTenants().catch((err) => {
      // Don't cache failures — allow the next caller to retry.
      if (cachedUserId === userId) cachedPromise = null
      throw err
    })
  }
  return cachedPromise
}

/**
 * Forces a fresh fetch, replacing the cached promise. Use for an explicit
 * "Refresh" affordance.
 */
export function refreshUserTenants(userId: string): Promise<UserTenant[]> {
  cachedUserId = userId
  cachedPromise = authApi.getUserTenants().catch((err) => {
    if (cachedUserId === userId) cachedPromise = null
    throw err
  })
  return cachedPromise
}

/**
 * Synchronously rewrites the cached list. Used after add/remove mutations so the
 * cache stays consistent without a network round-trip.
 */
export function setCachedUserTenants(userId: string, tenants: UserTenant[]): void {
  cachedUserId = userId
  cachedPromise = Promise.resolve(tenants)
}

/** Drops the cache entirely (e.g. on logout). */
export function clearUserTenantsCache(): void {
  cachedUserId = null
  cachedPromise = null
}
