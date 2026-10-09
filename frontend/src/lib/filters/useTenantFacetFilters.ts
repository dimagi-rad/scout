import { useCallback, useMemo, useState } from "react"

import type { UserTenant } from "@/api/auth"

import { sourceFiltersStorageKey } from "./sourceFilterStorage"
import {
  TENANT_FACETS,
  normalizeTenantSearch,
  parseTenantIdList,
  tenantMatchesSearch,
} from "./tenantFacets"
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

  // A pasted id list: which of its ids no source in `items` carries, so a typo or a
  // source the user cannot see is named instead of silently missing.
  const idList = useMemo(() => parseTenantIdList(normalized), [normalized])
  const unmatchedIds = useMemo(() => {
    if (!idList) return []
    const known = new Set(items.map((t) => t.tenant_id))
    return idList.filter((id) => !known.has(id))
  }, [idList, items])

  return {
    filtered: list.filtered,
    /** The ids of a pasted id list (null when the search is not one). */
    idList,
    unmatchedIds,
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
