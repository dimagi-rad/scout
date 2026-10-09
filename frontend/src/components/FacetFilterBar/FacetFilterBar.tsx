import { useId, useState } from "react"
import { ChevronDown } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { SearchFilterBar } from "@/components/SearchFilterBar/SearchFilterBar"
import type { FacetDef, FacetOption, FacetSelection } from "@/lib/filters/facets"
import { cn } from "@/lib/utils"

export const FACET_COLLAPSED_LIMIT = 12

interface FacetFilterBarProps<T> {
  /** Prefix for every data-testid, e.g. "create-sources-filter". */
  testIdPrefix: string
  search: string
  onSearchChange: (value: string) => void
  searchPlaceholder?: string
  facets: readonly FacetDef<T>[]
  options: Readonly<Record<string, FacetOption[]>>
  selection: FacetSelection
  onFacetChange: (key: string, values: string[]) => void
  onClear: () => void
  shownCount: number
  totalCount: number
}

export function FacetFilterBar<T>({
  testIdPrefix,
  search,
  onSearchChange,
  searchPlaceholder,
  facets,
  options,
  selection,
  onFacetChange,
  onClear,
  shownCount,
  totalCount,
}: FacetFilterBarProps<T>) {
  const ungrouped = facets.filter((f) => !f.group)
  const groups = [...new Set(facets.flatMap((f) => (f.group ? [f.group] : [])))]
  const canClear =
    search.trim() !== "" || Object.values(selection).some((values) => values.length > 0)

  const renderFacet = (def: FacetDef<T>) => (
    <FacetPopover
      key={def.key}
      def={def}
      options={options[def.key] ?? []}
      selected={selection[def.key] ?? []}
      onChange={(values) => onFacetChange(def.key, values)}
      testId={`${testIdPrefix}-facet-${def.key}`}
    />
  )

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <SearchFilterBar
        search={search}
        onSearchChange={onSearchChange}
        placeholder={searchPlaceholder}
        filters={[]}
        activeFilters={{}}
        onFilterChange={() => {}}
        orientation="stacked"
      />
      {facets.length > 0 && (
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          {ungrouped.map(renderFacet)}
          {groups.map((group) => (
            <div
              key={group}
              role="group"
              aria-label={`${group} filters`}
              className={cn("flex flex-wrap items-center gap-1.5", ungrouped.length > 0 && "ml-1.5")}
            >
              <span className="text-xs font-medium text-muted-foreground">{group}</span>
              {facets.filter((f) => f.group === group).map(renderFacet)}
            </div>
          ))}
        </div>
      )}
      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span data-testid={`${testIdPrefix}-count`} aria-live="polite">
          Showing {shownCount} of {totalCount}
        </span>
        {canClear && (
          <button
            type="button"
            onClick={onClear}
            className="underline-offset-2 hover:text-foreground hover:underline"
            data-testid={`${testIdPrefix}-clear`}
          >
            Clear filters
          </button>
        )}
      </div>
    </div>
  )
}

function buttonLabel<T>(def: FacetDef<T>, selected: readonly string[], options: FacetOption[]) {
  const name = def.shortLabel ?? def.label
  if (selected.length === 0) return name
  if (selected.length > 1) return `${name}: ${selected.length}`
  const label = options.find((o) => o.value === selected[0])?.label ?? selected[0]
  return `${name}: ${label}`
}

function FacetPopover<T>({
  def,
  options,
  selected,
  onChange,
  testId,
}: {
  def: FacetDef<T>
  options: FacetOption[]
  selected: readonly string[]
  onChange: (values: string[]) => void
  testId: string
}) {
  const idBase = useId()
  const [query, setQuery] = useState("")
  const [expanded, setExpanded] = useState(false)
  // Values selected when the popover opened stay listed while it is open, so
  // unticking one below the cut-off does not make it vanish under the cursor.
  const [pinned, setPinned] = useState<ReadonlySet<string>>(new Set())

  const selectedSet = new Set(selected)
  const active = selected.length > 0

  function handleOpenChange(open: boolean) {
    if (!open) return
    setQuery("")
    setExpanded(false)
    setPinned(new Set(selected))
  }

  function toggle(value: string) {
    onChange(
      selectedSet.has(value) ? selected.filter((v) => v !== value) : [...selected, value],
    )
  }

  const needle = query.trim().toLowerCase()
  const matching = needle
    ? options.filter(
        (o) => o.label.toLowerCase().includes(needle) || o.value.toLowerCase().includes(needle),
      )
    : options
  const collapsible = def.searchable && !needle && !expanded
  const visible = collapsible
    ? matching.filter((o, i) => i < FACET_COLLAPSED_LIMIT || pinned.has(o.value))
    : matching
  const hiddenCount = matching.length - visible.length

  return (
    <Popover onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="xs"
          className={cn(
            "max-w-56 font-normal",
            active && "border-primary/60 bg-primary/5 font-medium text-foreground",
          )}
          data-testid={testId}
          data-active={active || undefined}
        >
          <span className="truncate">{buttonLabel(def, selected, options)}</span>
          <ChevronDown aria-hidden />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        portal={false}
        align="start"
        className="w-72 p-1"
        data-testid={`${testId}-popover`}
      >
        {def.searchable && (
          <div className="p-1">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.preventDefault()
              }}
              placeholder={`Search ${def.label.toLowerCase()}…`}
              aria-label={`Search ${def.label.toLowerCase()}`}
              className="h-7 text-xs"
              data-testid={`${testId}-search`}
            />
          </div>
        )}
        <div className="max-h-64 overflow-y-auto" role="group" aria-label={def.label}>
          {visible.length === 0 ? (
            <p className="px-2 py-3 text-center text-xs text-muted-foreground">No matches</p>
          ) : (
            visible.map((option) => {
              const id = `${idBase}-${option.value}`
              return (
                <div
                  key={option.value}
                  className="group flex items-center gap-2 rounded-sm px-2 py-1 hover:bg-accent"
                >
                  <input
                    id={id}
                    type="checkbox"
                    className="size-3.5 shrink-0 accent-primary"
                    checked={selectedSet.has(option.value)}
                    onChange={() => toggle(option.value)}
                    data-testid={`${testId}-option-${option.value}`}
                  />
                  <label
                    htmlFor={id}
                    className={cn(
                      "min-w-0 flex-1 cursor-pointer truncate text-sm",
                      option.count === 0 && "text-muted-foreground",
                    )}
                    title={option.label}
                  >
                    {option.label}
                  </label>
                  {/* "Only" takes the count's place on hover or keyboard focus. */}
                  <span className="relative w-9 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                    <span className="group-hover:invisible group-has-[button:focus-visible]:invisible">
                      {option.count}
                    </span>
                    <button
                      type="button"
                      onClick={() => onChange([option.value])}
                      className="absolute inset-y-0 right-0 rounded px-1 opacity-0 hover:text-foreground hover:underline focus-visible:opacity-100 group-hover:opacity-100"
                      aria-label={`Only ${option.label}`}
                      data-testid={`${testId}-only-${option.value}`}
                    >
                      Only
                    </button>
                  </span>
                </div>
              )
            })
          )}
        </div>
        {(hiddenCount > 0 || active) && (
          <div className="flex items-center justify-between border-t px-2 pt-1 pb-0.5">
            {hiddenCount > 0 ? (
              <button
                type="button"
                onClick={() => setExpanded(true)}
                className="text-xs text-muted-foreground hover:text-foreground hover:underline"
                data-testid={`${testId}-show-more`}
              >
                Show {hiddenCount} more
              </button>
            ) : (
              <span />
            )}
            {active && (
              <button
                type="button"
                onClick={() => onChange([])}
                className="text-xs text-muted-foreground hover:text-foreground hover:underline"
                data-testid={`${testId}-reset`}
              >
                Clear
              </button>
            )}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
