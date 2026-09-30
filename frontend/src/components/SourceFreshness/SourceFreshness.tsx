import type { SourceFreshnessDetail, WorkspaceFreshness } from "@/api/workspaces"
import { formatRelativeTime } from "@/lib/relativeTime"

interface Props {
  freshness: WorkspaceFreshness | null
}

function freshnessLabel(source: SourceFreshnessDetail): string {
  if (!source.last_fetched_at) return "not loaded yet"
  // Loaded, but outside what the workspace queries (e.g. no live multi-source view).
  if (!source.serving) return "loaded but not in use"
  return `data as of ${formatRelativeTime(source.last_fetched_at)}`
}

/**
 * One "Data as of" line per source, so a stale source is not hidden by a fresh one.
 * Ages only the sources the workspace queries, as the stale-data banner does.
 */
export function SourceFreshness({ freshness }: Props) {
  const sources = freshness?.sources ?? []

  if (sources.length === 0) return null

  return (
    <ul
      className="flex flex-wrap gap-x-4 gap-y-0.5 px-4 pb-1 text-xs text-muted-foreground"
      data-testid="source-freshness"
    >
      {sources.map((source) => (
        <li key={source.tenant_id} data-testid={`source-freshness-${source.tenant_id}`}>
          {source.name}: {freshnessLabel(source)}
        </li>
      ))}
    </ul>
  )
}
