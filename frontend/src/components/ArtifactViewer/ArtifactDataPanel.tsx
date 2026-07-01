import { useState } from "react"
import { Loader2, RefreshCw } from "lucide-react"

import { Button } from "@/components/ui/button"
import type { QueryDataResponse, QueryResult } from "./types"

interface ArtifactDataPanelProps {
  queryData: QueryDataResponse | null
  isLoading: boolean
  error: string | null
  onRefresh: () => void
}

export function ArtifactDataPanel({
  queryData,
  isLoading,
  error,
  onRefresh,
}: ArtifactDataPanelProps) {
  return (
    <div className="flex-1 overflow-y-auto bg-background" data-testid="artifact-data-panel">
      <div className="space-y-4 p-4">
        <div className="flex justify-end">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onRefresh}
            disabled={isLoading}
            data-testid="artifact-data-refresh"
          >
            {isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
            Refresh
          </Button>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {isLoading && !queryData && (
          <div className="flex items-center justify-center py-12 text-muted-foreground">
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            <span className="text-sm">Executing queries...</span>
          </div>
        )}

        {queryData?.queries?.length === 0 && !isLoading && (
          <div className="py-12 text-center text-sm text-muted-foreground">
            This artifact has no stored queries. Data was embedded statically.
          </div>
        )}

        {queryData?.queries?.map((q, i) => (
          <QueryResultCard key={i} query={q} />
        ))}
      </div>
    </div>
  )
}

function QueryResultCard({ query }: { query: QueryResult }) {
  const [expanded, setExpanded] = useState(true)

  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between bg-muted/50 px-4 py-2.5 text-left transition-colors hover:bg-muted"
      >
        <span className="text-sm font-medium">{query.name}</span>
        <span className="text-xs text-muted-foreground">
          {query.error
            ? "Error"
            : `${query.row_count ?? 0} row${query.row_count === 1 ? "" : "s"}${query.truncated ? " (truncated)" : ""}`}
        </span>
      </button>

      {expanded && (
        <div className="divide-y divide-border">
          <div className="bg-muted/20 p-3">
            <pre className="overflow-x-auto whitespace-pre-wrap text-xs font-mono text-muted-foreground">
              {JSON.stringify(query.semantic_query ?? {}, null, 2)}
            </pre>
          </div>

          {query.error && (
            <div className="bg-destructive/5 p-3 text-sm text-destructive">
              {query.error}
            </div>
          )}

          {!query.error && query.columns && query.columns.length > 0 && (
            <div className="max-h-80 overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-muted/30">
                  <tr>
                    {query.columns.map((col) => (
                      <th
                        key={col}
                        className="whitespace-nowrap px-3 py-2 text-left font-medium text-muted-foreground"
                      >
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(query.rows ?? []).map((row, ri) => (
                    <tr key={ri} className="hover:bg-muted/20">
                      {(row as unknown[]).map((cell, ci) => (
                        <td key={ci} className="whitespace-nowrap px-3 py-1.5">
                          {cell === null ? (
                            <span className="italic text-muted-foreground">null</span>
                          ) : (
                            String(cell)
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
