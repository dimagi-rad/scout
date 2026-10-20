/**
 * Mirrors MAX_SUBSET in apps/users/services/tenant_listing/connect.py: up to this many
 * Connect opportunities, access is checked one opportunity at a time; above it, through
 * Connect's full export, which takes 10s+ for users with hundreds of opportunities (#902).
 */
export const CONNECT_QUICK_CHECK_LIMIT = 25

export function countConnectSources(sources: Iterable<{ provider: string }>): number {
  let count = 0
  // Matches the backend's canonical_provider, which reads any "commcare_connect*" as Connect.
  for (const source of sources) if (source.provider.startsWith("commcare_connect")) count += 1
  return count
}
