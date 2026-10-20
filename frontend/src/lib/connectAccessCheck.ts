import { CONNECT_PROVIDER } from "@/lib/filters/tenantFacets"

/**
 * Mirrors MAX_SUBSET in apps/users/services/tenant_listing/connect.py: up to this many
 * Connect opportunities, access is checked one opportunity at a time; above it, through
 * Connect's full export, which takes 10s+ for users with hundreds of opportunities (#902).
 */
export const CONNECT_QUICK_CHECK_LIMIT = 25

export function countConnectSources(sources: Iterable<{ provider: string }>): number {
  let count = 0
  for (const source of sources) if (source.provider === CONNECT_PROVIDER) count += 1
  return count
}
