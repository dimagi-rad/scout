import type { WorkspaceFreshness } from "@/api/workspaces"

const HOUR_MS = 3600_000
export const DEFAULT_STALE_HOURS = 24

export interface StaleData {
  ageLabel: string
  /** Named only when several sources serve, so the age is not read as everyone's. */
  oldestSourceName: string | null
  /** Providers of stale sources whose sign-in this viewer must renew before a refresh helps. */
  reconnectProviders: string[]
  /** At least one stale source can be fetched by a refresh as things stand. */
  refreshable: boolean
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
  const thresholdHours = freshness.stale_data_banner_hours ?? DEFAULT_STALE_HOURS
  // A non-positive threshold switches the banner off rather than showing it always.
  if (thresholdHours <= 0) return null
  const aged = serving.map((source) => ({
    source,
    ageMs: now - Date.parse(source.last_fetched_at as string),
  }))
  const stale = aged.filter(({ ageMs }) => ageMs >= thresholdHours * HOUR_MS)
  if (stale.length === 0) return null
  const oldest = stale.reduce((a, b) => (b.ageMs > a.ageMs ? b : a))
  const reconnectProviders = [
    ...new Set(
      stale
        .filter(({ source }) => source.reconnect)
        .map(({ source }) => source.provider_label || source.provider),
    ),
  ]
  return {
    ageLabel: formatDataAge(oldest.ageMs),
    oldestSourceName: serving.length > 1 ? oldest.source.name : null,
    reconnectProviders,
    refreshable: stale.some(({ source }) => !source.reconnect),
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
