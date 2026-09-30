import { useWorkspaceDetail } from "@/hooks/useWorkspaceDetail"
import { formatRelativeTime } from "@/lib/relativeTime"

interface Props {
  workspaceId: string
  /** A load is running. Refetched when it ends, since the data just changed. */
  loading?: boolean
}

/**
 * One "Data as of" line per source, so a stale source is not hidden by a fresh one.
 * Mount with `key={workspaceId}` (see useWorkspaceDetail).
 */
export function SourceFreshness({ workspaceId, loading = false }: Props) {
  const sources = useWorkspaceDetail(workspaceId, loading)?.sources ?? []

  if (sources.length === 0) return null

  return (
    <ul
      className="flex flex-wrap gap-x-4 gap-y-0.5 px-4 pb-1 text-xs text-muted-foreground"
      data-testid="source-freshness"
    >
      {sources.map((source) => (
        <li key={source.tenant_id} data-testid={`source-freshness-${source.tenant_id}`}>
          {source.tenant_name}:{" "}
          {source.last_synced_at
            ? `data as of ${formatRelativeTime(source.last_synced_at)}`
            : "not loaded yet"}
        </li>
      ))}
    </ul>
  )
}
