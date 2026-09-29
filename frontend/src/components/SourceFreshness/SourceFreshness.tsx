import { useEffect, useState } from "react"
import { workspaceApi, type WorkspaceSourceFreshness } from "@/api/workspaces"
import { formatRelativeTime } from "@/lib/relativeTime"

interface Props {
  workspaceId: string
  /** A load is running. Refetched when it ends, since the data just changed. */
  loading?: boolean
}

/**
 * One "Data as of" line per source, so a stale source is not hidden by a fresh one.
 * Mount with `key={workspaceId}`: a load blocks the refetch, which would otherwise
 * leave the previous workspace's lines up under the new one.
 */
export function SourceFreshness({ workspaceId, loading = false }: Props) {
  const [sources, setSources] = useState<WorkspaceSourceFreshness[]>([])

  useEffect(() => {
    if (loading) return
    let cancelled = false
    workspaceApi
      .getDetail(workspaceId)
      .then((detail) => {
        if (!cancelled) setSources(detail.sources ?? [])
      })
      .catch(() => {
        // Freshness is informational; the chat works without it.
      })
    return () => {
      cancelled = true
    }
  }, [workspaceId, loading])

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
