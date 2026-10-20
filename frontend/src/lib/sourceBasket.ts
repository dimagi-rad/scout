import type { UserTenant } from "@/api/auth"

// Users hold hundreds of sources (p90 ~750), so results show only after typing and
// stop here; the rest ask for a narrower search.
export const SOURCE_SEARCH_LIMIT = 50

export interface SourceSearchResult {
  matches: UserTenant[]
  /** Matches past the limit, not rendered. */
  hidden: number
}

export function searchSources(
  sources: UserTenant[],
  query: string,
  limit = SOURCE_SEARCH_LIMIT,
): SourceSearchResult {
  const q = query.trim().toLowerCase()
  if (!q) return { matches: [], hidden: 0 }
  const all = sources
    .filter((s) => s.tenant_name.toLowerCase().includes(q))
    .sort((a, b) => a.tenant_name.localeCompare(b.tenant_name))
  return { matches: all.slice(0, limit), hidden: Math.max(0, all.length - limit) }
}

const NAME_MAX = 80

/** A starting name for a workspace built from these sources; the user can edit it. */
export function combinedWorkspaceName(names: string[]): string {
  if (names.length === 0) return ""
  if (names.length === 1) return names[0]
  // Opportunities in one program usually share leading words ("CHC Nutrition Kenya").
  const words = names.map((n) => n.trim().split(/\s+/))
  const shared: string[] = []
  for (let i = 0; i < words[0].length; i++) {
    const word = words[0][i]
    if (!words.every((w) => w[i]?.toLowerCase() === word.toLowerCase())) break
    shared.push(word)
  }
  const prefix = shared.join(" ").replace(/[\s\-–—:,]+$/, "")
  const name =
    prefix.length >= 3
      ? `${prefix} (${names.length} sources)`
      : names.length === 2
        ? `${names[0]} + ${names[1]}`
        : `${names[0]} + ${names.length - 1} more`
  return name.length > NAME_MAX ? `${name.slice(0, NAME_MAX - 1)}…` : name
}
