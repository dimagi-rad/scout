import type { SourceFreshnessDetail, WorkspaceFreshness } from "@/api/workspaces"
import { formatRelativeTime } from "@/lib/relativeTime"
import { IMPORT_EXPLAINER } from "@/lib/importCopy"

import { LoadSummary } from "./LoadSummary"

interface Props {
  freshness: WorkspaceFreshness | null
  summaryOpen?: boolean
  onSummaryOpenChange?: (open: boolean) => void
  /** The summary opened itself because a load just finished. */
  justLoaded?: boolean
  /** Only the What's imported link: the layout already says how fresh the data is. */
  summaryOnly?: boolean
}

function freshnessLabel(source: SourceFreshnessDetail): string {
  if (!source.last_fetched_at) return "not imported yet"
  // Loaded, but outside what the workspace queries (e.g. no live multi-source view).
  if (!source.serving) return "imported, but not used by this workspace"
  return `data as of ${formatRelativeTime(source.last_fetched_at)}`
}

/**
 * One "Data as of" line per source, so a stale source is not hidden by a fresh one.
 * Ages only the sources the workspace queries, as the stale-data banner does.
 */
export function SourceFreshness({
  freshness,
  summaryOpen = false,
  onSummaryOpenChange,
  justLoaded = false,
  summaryOnly = false,
}: Props) {
  const sources = freshness?.sources ?? []

  if (sources.length === 0) return null

  const hasSummary = sources.some((source) => source.summary)
  if (summaryOnly && !hasSummary) return null

  return (
    <>
      {summaryOpen && hasSummary && onSummaryOpenChange && (
        <LoadSummary
          sources={sources}
          justLoaded={justLoaded}
          onClose={() => onSummaryOpenChange(false)}
        />
      )}
      <ul
        className="flex flex-wrap gap-x-4 gap-y-0.5 px-4 pb-1 text-xs text-muted-foreground"
        data-testid="source-freshness"
      >
        {!summaryOnly && (
          <>
            <li className="font-medium" title={IMPORT_EXPLAINER}>
              {sources.length === 1 ? "Data source:" : `${sources.length} data sources:`}
            </li>
            {sources.map((source) => (
              <li key={source.tenant_id} data-testid={`source-freshness-${source.tenant_id}`}>
                {source.name}: {freshnessLabel(source)}
              </li>
            ))}
          </>
        )}
        {hasSummary && onSummaryOpenChange && (
          <li>
            <button
              type="button"
              className="underline hover:text-foreground"
              onClick={() => onSummaryOpenChange(!summaryOpen)}
              aria-expanded={summaryOpen}
              data-testid="source-freshness-details"
            >
              {summaryOpen ? "Hide details" : "What's imported"}
            </button>
          </li>
        )}
      </ul>
    </>
  )
}
