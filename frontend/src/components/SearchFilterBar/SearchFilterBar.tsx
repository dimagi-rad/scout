import { Search } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"

export interface FilterOption {
  value: string
  label: string
  count?: number
}

export interface FilterGroup {
  name: string
  options: FilterOption[]
}

interface SearchFilterBarProps {
  search: string
  onSearchChange: (value: string) => void
  placeholder?: string
  filters: FilterGroup[]
  activeFilters: Record<string, string | null>
  onFilterChange: (group: string, value: string | null) => void
  /**
   * Layout of the search box relative to the filter chips.
   * - "responsive" (default): stacked on mobile, single row at `sm`+. Suits
   *   full-width page contexts.
   * - "stacked": search always above the chips. Use inside narrow containers
   *   (e.g. a modal) where the viewport is wide but the available width isn't,
   *   so the row layout would cramp the search box.
   */
  orientation?: "responsive" | "stacked"
}

export function SearchFilterBar({
  search,
  onSearchChange,
  placeholder = "Search...",
  filters,
  activeFilters,
  onFilterChange,
  orientation = "responsive",
}: SearchFilterBarProps) {
  const stacked = orientation === "stacked"
  return (
    <div
      className={
        stacked
          ? "flex min-w-0 flex-col gap-3"
          : "flex min-w-0 flex-col gap-4 sm:flex-row sm:items-center"
      }
    >
      <div className="relative min-w-0 flex-1">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder={placeholder}
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          className="pl-9"
          data-testid="search-filter-input"
        />
      </div>
      {filters.map((group) => (
        <div
          key={group.name}
          // Chips never wrap and never shrink, so toggling a filter can't push
          // them onto a new row or collapse the search box. If they ever exceed
          // the available width they scroll horizontally — honest and contained.
          className="flex shrink-0 gap-2 overflow-x-auto"
        >
          <Button
            variant={activeFilters[group.name] == null ? "default" : "outline"}
            size="sm"
            // The `default` (active) variant has no border while `outline`
            // (inactive) carries a 1px border, so toggling state would change
            // the chip's box width and reflow the whole row. Reserve a 1px
            // transparent border on the active chip so its width is identical
            // to the inactive state. Width stays constant — no reflow.
            className={activeFilters[group.name] == null ? "border border-transparent" : ""}
            onClick={() => onFilterChange(group.name, null)}
            data-testid={`filter-${group.name}-all`}
          >
            All
          </Button>
          {group.options.map((opt) => {
            const isActive = activeFilters[group.name] === opt.value
            return (
              <Button
                key={opt.value}
                variant={isActive ? "default" : "outline"}
                size="sm"
                className={isActive ? "border border-transparent" : ""}
                onClick={() => onFilterChange(group.name, opt.value)}
                data-testid={`filter-${group.name}-${opt.value}`}
              >
                {opt.label}
                {opt.count != null && (
                  <span className="ml-1 text-xs opacity-60">{opt.count}</span>
                )}
              </Button>
            )
          })}
        </div>
      ))}
    </div>
  )
}
