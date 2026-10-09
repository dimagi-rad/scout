import { useCallback, useMemo } from "react"

import {
  applyFacets,
  availableFacets,
  computeFacetOptions,
  effectiveSelection,
  isFiltering,
  type FacetDef,
} from "./facets"
import { usePersistentFacetSelection } from "./facetStorage"

/**
 * Facet filtering over an in-memory list, persisted per `storageKey`.
 * `predicate` (typically the text search) must be memoized by the caller.
 */
export function useFacetedList<T>({
  items,
  facets,
  storageKey,
  predicate,
}: {
  items: readonly T[]
  facets: readonly FacetDef<T>[]
  storageKey: string | null
  predicate: (item: T) => boolean
}) {
  const knownKeys = useMemo(() => facets.map((f) => f.key), [facets])
  const [stored, setStored] = usePersistentFacetSelection(storageKey, knownKeys)

  const visibleFacets = useMemo(() => availableFacets(facets, items), [facets, items])
  const selection = useMemo(
    () => effectiveSelection(stored, visibleFacets, items),
    [stored, visibleFacets, items],
  )
  const filtered = useMemo(
    () => applyFacets(items, visibleFacets, selection, predicate),
    [items, visibleFacets, selection, predicate],
  )
  const options = useMemo(
    () => computeFacetOptions(items, visibleFacets, selection, predicate),
    [items, visibleFacets, selection, predicate],
  )

  // Stored values absent from this list (another picker's list may still have
  // them) survive a toggle; `replace` ("Only", per-facet clear) drops them too.
  const setFacet = useCallback(
    (key: string, values: readonly string[], replace = false) => {
      const present = new Set((options[key] ?? []).map((o) => o.value))
      const elsewhere = replace ? [] : (stored[key] ?? []).filter((v) => !present.has(v))
      setStored({ ...stored, [key]: [...values, ...elsewhere] })
    },
    [stored, setStored, options],
  )
  const clearFacets = useCallback(() => setStored({}), [setStored])

  return {
    facets: visibleFacets,
    selection,
    options,
    filtered,
    setFacet,
    clearFacets,
    filtering: isFiltering(selection),
  }
}
