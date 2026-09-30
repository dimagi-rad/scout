import { workspaceApi } from "@/api/workspaces"
import { useRefetchOnLoadEnd } from "@/hooks/useRefetchOnLoadEnd"
import { formatRelativeTime } from "@/lib/relativeTime"

interface Props {
  workspaceId: string
  /** A load is running. Refetched when it ends, since the data just changed. */
  loading?: boolean
}

/**
 * One "Data as of" line per source, so a stale source is not hidden by a fresh one.
 * Reads the serving snapshot's age, the same figure the stale-data banner judges by.
 * Mount with `key={workspaceId}` (see useRefetchOnLoadEnd).
 */
export function SourceFreshness({ workspaceId, loading = false }: Props) {
  const sources = useRefetchOnLoadEnd(workspaceApi.getFreshness, workspaceId, loading)?.sources ?? []

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
