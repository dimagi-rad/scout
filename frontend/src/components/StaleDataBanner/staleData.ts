import type { WorkspaceFreshness } from "@/api/workspaces"

const HOUR_MS = 3600_000
export const DEFAULT_STALE_HOURS = 24

export interface StaleData {
  ageLabel: string
  /** Named only when several sources serve, so the age is not read as everyone's. */
  oldestSourceName: string | null
  /** Providers whose sign-in this viewer must renew before a refresh can fetch them. */
  reconnectProviders: string[]
}

/** "5 hours ago" under 48 hours, "3 days ago" after. */
export function formatDataAge(ageMs: number): string {
  const hours = Math.max(0, Math.floor(ageMs / HOUR_MS))
  if (hours < 48) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`
  const days = Math.floor(hours / 24)
  return `${days} days ago`
}

/**
 * Whether the oldest serving source is past the workspace's threshold, and what
 * to offer. Null while a load runs (its own banner covers it), when nothing is
 * loaded yet (the not-loaded flow covers it), or when the freshness request failed
 * (a member who lost access is refused it).
 */
export function staleData(
  freshness: WorkspaceFreshness | null,
  { loading = false, now = Date.now() }: { loading?: boolean; now?: number } = {},
): StaleData | null {
  if (!freshness || loading || freshness.in_progress) return null
  const serving = freshness.sources.filter((source) => source.serving && source.last_fetched_at)
  if (serving.length === 0) return null
  const times = serving.map((source) => Date.parse(source.last_fetched_at as string))
  const oldestTime = Math.min(...times)
  const oldest = serving[times.indexOf(oldestTime)]
  const ageMs = now - oldestTime
  const thresholdHours = freshness.stale_data_banner_hours ?? DEFAULT_STALE_HOURS
  if (!(ageMs >= thresholdHours * HOUR_MS)) return null
  const reconnectProviders = [
    ...new Set(
      freshness.sources
        .filter((source) => source.reconnect)
        .map((source) => source.provider_label || source.provider),
    ),
  ]
  return {
    ageLabel: formatDataAge(ageMs),
    oldestSourceName: serving.length > 1 ? oldest.name : null,
    reconnectProviders,
  }
}

function dismissKey(workspaceId: string): string {
  return `scout:stale-banner-dismissed:${workspaceId}`
}

export function isStaleBannerDismissed(workspaceId: string): boolean {
  try {
    return sessionStorage.getItem(dismissKey(workspaceId)) === "1"
  } catch {
    return false
  }
}

export function dismissStaleBanner(workspaceId: string): void {
  try {
    sessionStorage.setItem(dismissKey(workspaceId), "1")
  } catch {
    // Storage may be unavailable (private mode, quota); the dismiss still holds until remount.
  }
}
