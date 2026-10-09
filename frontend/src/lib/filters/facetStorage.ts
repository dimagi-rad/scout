import { useCallback, useState } from "react"

import type { FacetSelection } from "./facets"

// Bounds what a tampered or runaway entry can make us hold in memory.
const MAX_VALUES_PER_FACET = 500
const MAX_VALUE_LENGTH = 200

/**
 * Parses a stored selection, keeping only known facet keys and string values.
 * Anything malformed reads as "no filters" rather than throwing.
 */
export function parseFacetSelection(
  raw: string | null,
  knownKeys: readonly string[],
): Record<string, string[]> {
  if (!raw) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return {}
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {}
  const record = parsed as Record<string, unknown>
  const result: Record<string, string[]> = {}
  for (const key of knownKeys) {
    if (!Object.hasOwn(record, key)) continue
    const values = record[key]
    if (!Array.isArray(values)) continue
    const kept = [
      ...new Set(
        values.filter(
          (v): v is string => typeof v === "string" && v.length > 0 && v.length <= MAX_VALUE_LENGTH,
        ),
      ),
    ].slice(0, MAX_VALUES_PER_FACET)
    if (kept.length > 0) result[key] = kept
  }
  return result
}

export function readFacetSelection(
  storageKey: string,
  knownKeys: readonly string[],
): Record<string, string[]> {
  try {
    return parseFacetSelection(localStorage.getItem(storageKey), knownKeys)
  } catch {
    return {}
  }
}

export function writeFacetSelection(storageKey: string, selection: FacetSelection): void {
  const nonEmpty = Object.fromEntries(
    Object.entries(selection).filter(([, values]) => values.length > 0),
  )
  try {
    if (Object.keys(nonEmpty).length === 0) localStorage.removeItem(storageKey)
    else localStorage.setItem(storageKey, JSON.stringify(nonEmpty))
  } catch {
    // Storage may be unavailable (private mode, quota). Persistence is best-effort.
  }
}

/**
 * A facet selection persisted under `storageKey`; a null key keeps it in memory only
 * (e.g. before the user id is known). Re-reads when the key changes, so an account
 * switch never shows the previous user's filters.
 */
export function usePersistentFacetSelection(
  storageKey: string | null,
  knownKeys: readonly string[],
): [FacetSelection, (next: FacetSelection) => void] {
  const [state, setState] = useState<{ key: string | null; selection: FacetSelection }>(() => ({
    key: storageKey,
    selection: storageKey ? readFacetSelection(storageKey, knownKeys) : {},
  }))
  let current = state
  if (state.key !== storageKey) {
    current = {
      key: storageKey,
      selection: storageKey ? readFacetSelection(storageKey, knownKeys) : {},
    }
    setState(current)
  }

  const setSelection = useCallback(
    (next: FacetSelection) => {
      setState({ key: storageKey, selection: next })
      if (storageKey) writeFacetSelection(storageKey, next)
    },
    [storageKey],
  )

  return [current.selection, setSelection]
}
