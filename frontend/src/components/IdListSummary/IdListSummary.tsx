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
    ...notFoundGroups,
    { key: "unrecognised", label: "not an ID", ids: report.unrecognised },
  ].filter((group) => group.ids.length > 0)
  const total = report.ids.length

  return (
    <div className="space-y-1 text-xs text-muted-foreground" data-testid={testIdPrefix}>
      <div className="flex items-center justify-between gap-2">
        <span data-testid={`${testIdPrefix}-shown`}>
          {report.shown.length === total && report.unrecognised.length === 0
            ? `All ${total} IDs shown`
            : `${report.shown.length} of ${total} IDs shown`}
        </span>
        {action}
      </div>
      {report.hiddenByFilters.length > 0 && (
        <p data-testid={`${testIdPrefix}-hidden`}>
          {report.hiddenByFilters.length} hidden by filters:{" "}
          {formatIdSample(report.hiddenByFilters)}{" "}
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={onClearFacets}
            data-testid={`${testIdPrefix}-clear-facets`}
          >
            Clear filters
          </Button>
        </p>
      )}
      {missing.map((group) => (
        <p key={group.key} data-testid={`${testIdPrefix}-${group.key}`}>
          {group.ids.length} {group.label}: {formatIdSample(group.ids)}
        </p>
      ))}
    </div>
  )
}
