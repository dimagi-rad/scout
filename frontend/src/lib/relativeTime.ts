const SECOND = 1
const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" })

/**
 * Format an ISO timestamp as a relative phrase like "12 minutes ago".
 * Returns "just now" for differences under 30 seconds, falls back to a date
 * string for differences over ~30 days.
 */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso)
  const deltaSeconds = Math.round((then.getTime() - now.getTime()) / 1000)
  const abs = Math.abs(deltaSeconds)

  if (abs < 30 * SECOND) return "just now"
  if (abs < HOUR) return rtf.format(Math.round(deltaSeconds / MINUTE), "minute")
  if (abs < DAY) return rtf.format(Math.round(deltaSeconds / HOUR), "hour")
  if (abs < 30 * DAY) return rtf.format(Math.round(deltaSeconds / DAY), "day")
  return then.toLocaleDateString()
}
