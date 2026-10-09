import { clearStoredFacetSelections } from "./facetStorage"

// Kept apart from tenantFacets so the store can clear filters without importing
// the provider icon modules.
export const SOURCE_FILTERS_STORAGE_PREFIX = "scout:source-filters:v1:"

/** Facet selections are shared by the create-workspace and add-source pickers. */
export function sourceFiltersStorageKey(userId: string | undefined): string | null {
  return userId ? `${SOURCE_FILTERS_STORAGE_PREFIX}${userId}` : null
}

/** On logout or account switch, mirroring the composer drafts. */
export function clearAllSourceFilters(): void {
  clearStoredFacetSelections(SOURCE_FILTERS_STORAGE_PREFIX)
}

/** Selections another account left behind (e.g. a session that expired while closed). */
export function clearOtherUsersSourceFilters(userId: string): void {
  clearStoredFacetSelections(SOURCE_FILTERS_STORAGE_PREFIX, sourceFiltersStorageKey(userId))
}
