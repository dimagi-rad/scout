import type { WorkspaceFreshness } from "@/api/workspaces"
import { formatRelativeTime } from "@/lib/relativeTime"

interface Props {
  freshness: WorkspaceFreshness | null
}

/**
 * One "Data as of" line per source, so a stale source is not hidden by a fresh one.
 * Reads the serving snapshot's age, the same figure the stale-data banner judges by.
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
          {source.name}:{" "}
          {source.last_fetched_at
            ? `data as of ${formatRelativeTime(source.last_fetched_at)}`
            : "not loaded yet"}
        </li>
      ))}
    </ul>
  )
}
