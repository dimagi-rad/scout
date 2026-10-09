import { useCallback, useState } from "react"

import type { UserTenant } from "@/api/auth"

import { sourceFiltersStorageKey } from "./sourceFilterStorage"
import { TENANT_FACETS, normalizeTenantSearch, tenantMatchesSearch } from "./tenantFacets"
import { useFacetedList } from "./useFacetedList"

/**
 * Search plus persisted facets for a data source picker. Both pickers use this so
 * their facets, storage key and clear semantics cannot drift apart.
 */
export function useTenantFacetFilters({
  items,
  userId,
  listIsComplete = false,
}: {
  items: readonly UserTenant[]
  userId: string | undefined
  /** True when `items` is every source the user has (see useFacetedList). */
  listIsComplete?: boolean
}) {
  const [search, setSearch] = useState("")
  const normalized = normalizeTenantSearch(search)
  const predicate = useCallback(
    (t: UserTenant) => tenantMatchesSearch(t, normalized),
    [normalized],
  )
  const list = useFacetedList({
    items,
    facets: TENANT_FACETS,
    storageKey: sourceFiltersStorageKey(userId),
    predicate,
    listIsComplete,
  })
  const { clearFacets } = list
  const clearFilters = useCallback(() => {
    setSearch("")
    clearFacets()
  }, [clearFacets])

  return {
    filtered: list.filtered,
    setSearch,
    clearFilters,
    /** Spread onto FacetFilterBar alongside its testIdPrefix. */
    barProps: {
      search,
      onSearchChange: setSearch,
      searchPlaceholder: "Search by name or opportunity ID…",
      facets: list.facets,
      options: list.options,
      selection: list.selection,
      onFacetChange: list.setFacet,
      onClear: clearFilters,
      shownCount: list.filtered.length,
      totalCount: items.length,
    },
  }
}
