import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { formatIdSample } from "@/lib/filters/tenantFacets"
import type { TenantIdReport } from "@/lib/filters/useTenantFacetFilters"

export interface IdGroup {
  key: string
  label: string
  ids: readonly string[]
}

/**
 * Where each id of a pasted list went (#900). Every id the list does not show is
 * named with its reason, so a typo or a remembered facet never drops one silently.
 */
export function IdListSummary({
  report,
  testIdPrefix,
  notFoundGroups = [{ key: "not-found", label: "not found", ids: report.notFound }],
  onClearFacets,
  action,
}: {
  report: TenantIdReport
  testIdPrefix: string
  /** Lets a picker split "not found" by cause; defaults to one group. */
  notFoundGroups?: readonly IdGroup[]
  onClearFacets: () => void
  action?: ReactNode
}) {
  const missing = [
    { key: "hidden", label: "hidden by filters", ids: report.hiddenByFilters },
    ...notFoundGroups,
    { key: "unrecognised", label: "not an ID", ids: report.unrecognised },
  ].filter((group) => group.ids.length > 0)
  const total = report.ids.length

  return (
    <div
      className="flex items-start justify-between gap-2 text-xs text-muted-foreground"
      data-testid={testIdPrefix}
    >
      {/* The live region holds text only: buttons inside one get re-announced as it updates. */}
      <div role="status" aria-atomic="false" className="min-w-0 space-y-1">
        <p data-testid={`${testIdPrefix}-shown`}>
          {report.shown.length < total
            ? `${report.shown.length} of ${total} IDs shown`
            : report.unrecognised.length > 0
              ? `${total} IDs shown`
              : `All ${total} IDs shown`}
        </p>
        {missing.map((group) => (
          <p key={group.key} className="break-words" data-testid={`${testIdPrefix}-${group.key}`}>
            {group.ids.length} {group.label}: {formatIdSample(group.ids)}
          </p>
        ))}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        {action}
        {report.hiddenByFilters.length > 0 && (
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={onClearFacets}
            data-testid={`${testIdPrefix}-clear-facets`}
          >
            {/* Not "Clear filters": the search bar has that, and it also drops the pasted list. */}
            Show hidden
          </Button>
        )}
      </div>
    </div>
  )
}
