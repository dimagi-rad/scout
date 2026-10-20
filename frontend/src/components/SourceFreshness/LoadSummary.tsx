import { X } from "lucide-react"

import type { LoadSummaryTable, SourceFreshnessDetail } from "@/api/workspaces"
import { Button } from "@/components/ui/button"
import { formatRelativeTime } from "@/lib/relativeTime"

interface Props {
  sources: SourceFreshnessDetail[]
  /** A load just finished, rather than the viewer asking what is loaded. */
  justLoaded: boolean
  onClose: () => void
}

const STATE_LABELS: Record<string, string> = {
  failed: "failed",
  cancelled: "cancelled",
  skipped: "not loaded",
  in_progress: "incomplete",
}

// UTC: a date-only event time (Connect's visit_date) must not shift a day west of UTC.
const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
})

function formatDate(value: string | null | undefined): string | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : DATE_FORMAT.format(date)
}

function dateRange(table: LoadSummaryTable): string | null {
  const earliest = formatDate(table.earliest)
  const latest = formatDate(table.latest)
  if (!earliest || !latest) return null
  return earliest === latest ? earliest : `${earliest} – ${latest}`
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`
}

/** Per source: each table's row count and date range from the serving snapshot's load. */
export function LoadSummary({ sources, justLoaded, onClose }: Props) {
  return (
    <section
      className="mx-4 mb-2 rounded-lg border bg-muted/30 p-3 text-sm"
      aria-label="Loaded data summary"
      data-testid="load-summary"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="font-medium">{justLoaded ? "Data load finished" : "What's loaded"}</p>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0 text-muted-foreground"
          onClick={onClose}
          aria-label="Close loaded data summary"
          data-testid="load-summary-close"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="max-h-64 space-y-3 overflow-y-auto">
        {sources.map((source) => {
          const summary = source.summary
          const completed = summary?.tables.filter((t) => t.state === "completed") ?? []
          return (
            <div key={source.tenant_id} data-testid={`load-summary-source-${source.tenant_id}`}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium">{source.name}</span>
                <span className="text-xs text-muted-foreground">{source.provider_label}</span>
              </div>
              {!summary || !source.last_fetched_at ? (
                <p className="text-xs text-muted-foreground">Not loaded yet.</p>
              ) : (
                <>
                  <p className="text-xs text-muted-foreground">
                    {plural(summary.total_rows, "row")} in {plural(completed.length, "table")} ·
                    loaded {formatRelativeTime(source.last_fetched_at)}
                    {!source.serving && " · not in use by this workspace"}
                  </p>
                  <table className="mt-1 w-full text-xs">
                    <tbody>
                      {summary.tables.map((table) => {
                        const range = dateRange(table)
                        const problem = STATE_LABELS[table.state]
                        return (
                          <tr
                            key={table.name}
                            data-testid={`load-summary-table-${source.tenant_id}-${table.name}`}
                          >
                            <td className="py-0.5 pr-3 font-mono">{table.name}</td>
                            <td className="py-0.5 pr-3 text-right tabular-nums">
                              {table.rows === null ? "—" : table.rows.toLocaleString()}
                            </td>
                            <td className="py-0.5 text-muted-foreground">
                              {problem ? (
                                <span className="text-destructive">{problem}</span>
                              ) : (
                                range
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
