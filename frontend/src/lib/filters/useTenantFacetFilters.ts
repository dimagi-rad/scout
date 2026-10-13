import { useCallback, useMemo, useState } from "react"

import type { UserTenant } from "@/api/auth"

import { sourceFiltersStorageKey } from "./sourceFilterStorage"
import {
  CONNECT_PROVIDER,
  TENANT_FACETS,
  normalizeTenantSearch,
  parseTenantIdList,
  tenantMatchesSearch,
} from "./tenantFacets"
import { useFacetedList } from "./useFacetedList"

export interface TenantIdReport {
  ids: readonly string[]
  shown: readonly string[]
  /** Sources the user has that the active facets hide. */
  hiddenByFilters: readonly string[]
  /** Not among `items`: a typo, or not a source this picker can offer. */
  notFound: readonly string[]
  unrecognised: readonly string[]
}

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
  // Parsed from the raw text so an unrecognised token is echoed as typed.
  const idList = useMemo(() => parseTenantIdList(search), [search])
  const predicate = useCallback(
    (t: UserTenant) => tenantMatchesSearch(t, normalized, idList),
    [normalized, idList],
  )
  const list = useFacetedList({
    items,
    facets: TENANT_FACETS,
    storageKey: sourceFiltersStorageKey(userId),
    predicate,
    listIsComplete,
  })
  const { clearFacets, filtered } = list
  const clearFilters = useCallback(() => {
    setSearch("")
    clearFacets()
  }, [clearFacets])

  // Where each pasted id went, so none drops out silently: a source hidden by a
  // remembered facet reads differently from a typo or a source the user lacks.
  const idReport = useMemo((): TenantIdReport | null => {
    if (!idList) return null
    const shownIds = new Set(filtered.map((t) => t.tenant_id))
    const listedIds = new Set(
      items.filter((t) => t.provider === CONNECT_PROVIDER).map((t) => t.tenant_id),
    )
    return {
      ids: idList.ids,
      shown: idList.ids.filter((id) => shownIds.has(id)),
      hiddenByFilters: idList.ids.filter((id) => listedIds.has(id) && !shownIds.has(id)),
      notFound: idList.ids.filter((id) => !listedIds.has(id)),
      unrecognised: idList.unrecognised,
    }
  }, [idList, items, filtered])

  return {
    filtered,
    /** Null unless the search is a pasted id list. */
    idReport,
    setSearch,
    clearFilters,
    clearFacets,
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
