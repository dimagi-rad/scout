import { useCallback, useMemo } from "react"

import {
  applyFacets,
  availableFacets,
  computeFacetOptions,
  effectiveSelection,
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
  listIsComplete = false,
}: {
  items: readonly T[]
  facets: readonly FacetDef<T>[]
  storageKey: string | null
  predicate: (item: T) => boolean
  /**
   * Whether `items` covers every value any picker sharing `storageKey` can show.
   * Only then may writes drop stored values absent from it.
   */
  listIsComplete?: boolean
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

  // A partial list (the add-source panel lacks sources already in the workspace)
  // keeps stored values it cannot see, so its writes never wipe what the other
  // picker applies. `replace` ("Only") overwrites the facet outright.
  const absentStored = useCallback(
    (key: string) => {
      if (listIsComplete) return []
      const present = new Set((options[key] ?? []).map((o) => o.value))
      return (stored[key] ?? []).filter((v) => !present.has(v))
    },
    [listIsComplete, options, stored],
  )
  const setFacet = useCallback(
    (key: string, values: readonly string[], replace = false) => {
      setStored({ ...stored, [key]: [...values, ...(replace ? [] : absentStored(key))] })
    },
    [stored, setStored, absentStored],
  )
  // Only facets on offer: one hidden for this data (e.g. Connect facets while the API
  // sends no attributes) keeps its stored selection for when it shows again.
  const clearFacets = useCallback(() => {
    const next: Record<string, readonly string[]> = { ...stored }
    for (const facet of visibleFacets) next[facet.key] = absentStored(facet.key)
    setStored(next)
  }, [stored, setStored, visibleFacets, absentStored])

  return {
    facets: visibleFacets,
    selection,
    options,
    filtered,
    setFacet,
    clearFacets,
  }
}
