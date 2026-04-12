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
}

export function SearchFilterBar({
  search,
  onSearchChange,
  placeholder = "Search...",
  filters,
  activeFilters,
  onFilterChange,
}: SearchFilterBarProps) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
      <div className="relative flex-1">
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
        <div key={group.name} className="flex flex-wrap gap-2">
          <Button
            variant={activeFilters[group.name] == null ? "default" : "outline"}
            size="sm"
            onClick={() => onFilterChange(group.name, null)}
            data-testid={`filter-${group.name}-all`}
          >
            All
          </Button>
          {group.options.map((opt) => (
            <Button
              key={opt.value}
              variant={activeFilters[group.name] === opt.value ? "default" : "outline"}
              size="sm"
              onClick={() => onFilterChange(group.name, opt.value)}
              data-testid={`filter-${group.name}-${opt.value}`}
            >
              {opt.label}
              {opt.count != null && (
                <span className="ml-1 text-xs opacity-60">{opt.count}</span>
              )}
            </Button>
          ))}
        </div>
      ))}
    </div>
  )
}
