/**
 * Client-side faceted filtering: AND across facets, OR within a facet.
 *
 * A selection lists the values to KEEP. An empty or missing entry means the facet
 * is not filtering. Storing exclusions instead ("none of") made Langfuse's
 * equivalent filter misbehave whenever a new value appeared, so it is avoided here.
 */

export interface FacetDef<T> {
  key: string
  label: string
  /** Compact button label, e.g. "Org" (and "Org: 3" when active). Defaults to `label`. */
  shortLabel?: string
  /** Facets sharing a group render together under that heading. */
  group?: string
  /**
   * The item's value for this facet, or undefined when the facet does not apply to
   * the item. Not-applicable items are never hidden by this facet and are not counted.
   */
  getValue: (item: T) => string | undefined
  /**
   * Display label for a value, from one item carrying it. Return undefined when this
   * item has no name for the value; another item may, and the raw value is the fallback.
   */
  optionLabel?: (value: string, item: T) => string | undefined
  /** Fixed display order; values not listed sort after, by label. */
  valueOrder?: readonly string[]
  /** Values that always sort last (e.g. "None"). */
  trailingValues?: readonly string[]
  /** Long lists get a search box and a "Show more" cut-off. */
  searchable?: boolean
  /** Whether to offer the facet at all for these items. Defaults to "has at least one value". */
  isAvailable?: (items: readonly T[]) => boolean
}

export type FacetSelection = Readonly<Record<string, readonly string[]>>

/**
 * A facet value made safe for a data-testid selector. A value that had to change
 * gets a hash of the original, so distinct values keep distinct ids.
 */
export function facetTestIdValue(value: string): string {
  const safe = value.replace(/[^A-Za-z0-9_-]+/g, "")
  if (safe === value) return value
  let hash = 0
  for (const ch of value) hash = (Math.imul(hash, 31) + ch.codePointAt(0)!) | 0
  return `${safe || "value"}-${(hash >>> 0).toString(36)}`
}

export interface FacetOption {
  value: string
  label: string
  /** Items with this value that pass the search and every OTHER facet. */
  count: number
}

function selectedSet(selection: FacetSelection, key: string): Set<string> | null {
  const values = selection[key]
  return values && values.length > 0 ? new Set(values) : null
}

function matchesFacet<T>(item: T, def: FacetDef<T>, selected: Set<string> | null): boolean {
  if (!selected) return true
  const value = def.getValue(item)
  return value === undefined || selected.has(value)
}

function matchesAll<T>(
  item: T,
  defs: readonly FacetDef<T>[],
  sets: ReadonlyMap<string, Set<string> | null>,
  skipKey?: string,
): boolean {
  for (const def of defs) {
    if (def.key === skipKey) continue
    if (!matchesFacet(item, def, sets.get(def.key) ?? null)) return false
  }
  return true
}

function selectionSets<T>(
  defs: readonly FacetDef<T>[],
  selection: FacetSelection,
): Map<string, Set<string> | null> {
  return new Map(defs.map((def) => [def.key, selectedSet(selection, def.key)]))
}

/** The facets worth showing for these items. */
export function availableFacets<T>(
  defs: readonly FacetDef<T>[],
  items: readonly T[],
): FacetDef<T>[] {
  return defs.filter((def) =>
    def.isAvailable
      ? def.isAvailable(items)
      : items.some((item) => def.getValue(item) !== undefined),
  )
}

/**
 * Restricts a (possibly persisted) selection to the given facets and to values that
 * exist in `items`. Stale values — a revoked org, a facet that no longer shows —
 * are dropped rather than silently hiding everything.
 */
export function effectiveSelection<T>(
  selection: FacetSelection,
  defs: readonly FacetDef<T>[],
  items: readonly T[],
): Record<string, string[]> {
  const result: Record<string, string[]> = {}
  for (const def of defs) {
    const wanted = selection[def.key]
    if (!wanted || wanted.length === 0) continue
    const present = new Set<string>()
    for (const item of items) {
      const value = def.getValue(item)
      if (value !== undefined) present.add(value)
    }
    const kept = wanted.filter((value) => present.has(value))
    if (kept.length > 0) result[def.key] = kept
  }
  return result
}

/** Items passing `predicate` (e.g. the text search) and every facet. */
export function applyFacets<T>(
  items: readonly T[],
  defs: readonly FacetDef<T>[],
  selection: FacetSelection,
  predicate: (item: T) => boolean = () => true,
): T[] {
  const sets = selectionSets(defs, selection)
  return items.filter((item) => predicate(item) && matchesAll(item, defs, sets))
}

function compareOptions(def: FacetDef<unknown>, a: FacetOption, b: FacetOption): number {
  const trailing = def.trailingValues ?? []
  const ta = trailing.includes(a.value)
  const tb = trailing.includes(b.value)
  if (ta !== tb) return ta ? 1 : -1
  const order = def.valueOrder ?? []
  const ia = order.indexOf(a.value)
  const ib = order.indexOf(b.value)
  if (ia !== ib) {
    if (ia === -1) return 1
    if (ib === -1) return -1
    return ia - ib
  }
  return a.label.localeCompare(b.label) || a.value.localeCompare(b.value)
}

/**
 * Every value each facet takes in `items`, with a live count computed against the
 * search and all OTHER facets — so a facet's own selection never zeroes its siblings.
 */
export function computeFacetOptions<T>(
  items: readonly T[],
  defs: readonly FacetDef<T>[],
  selection: FacetSelection,
  predicate: (item: T) => boolean = () => true,
): Record<string, FacetOption[]> {
  const sets = selectionSets(defs, selection)
  const searched = items.map(predicate)
  const result: Record<string, FacetOption[]> = {}
  for (const def of defs) {
    const options = new Map<string, FacetOption>()
    // Values still awaiting a display name: the first row may lack one a later row has.
    const unnamed = new Set<string>()
    items.forEach((item, index) => {
      const value = def.getValue(item)
      if (value === undefined) return
      let option = options.get(value)
      if (!option) {
        option = { value, label: value, count: 0 }
        options.set(value, option)
        if (def.optionLabel) unnamed.add(value)
      }
      if (unnamed.has(value)) {
        const label = def.optionLabel?.(value, item)
        if (label !== undefined) {
          option.label = label
          unnamed.delete(value)
        }
      }
      if (searched[index] && matchesAll(item, defs, sets, def.key)) option.count++
    })
    result[def.key] = [...options.values()].sort((a, b) =>
      compareOptions(def as FacetDef<unknown>, a, b),
    )
  }
  return result
}
