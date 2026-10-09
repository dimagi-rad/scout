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

  // Writes only the touched facet, so values another picker's list still has
  // (but this one lacks) survive in storage.
  const setFacet = useCallback(
    (key: string, values: readonly string[]) => setStored({ ...stored, [key]: values }),
    [stored, setStored],
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
