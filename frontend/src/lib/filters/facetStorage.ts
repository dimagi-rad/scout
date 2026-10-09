import { useCallback, useState, useSyncExternalStore } from "react"

import type { FacetSelection } from "./facets"

// Bounds what a tampered or runaway entry can make us hold in memory.
const MAX_VALUES_PER_FACET = 500
const MAX_VALUE_LENGTH = 255

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
 * Removes every stored selection under `prefix` except `keepKey`. Org and program
 * names identify the user's work, so they must not outlive the session on a shared browser.
 */
export function clearStoredFacetSelections(prefix: string, keepKey?: string | null): void {
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(prefix) && key !== keepKey) doomed.push(key)
    }
    for (const key of doomed) {
      localStorage.removeItem(key)
      notify(key)
    }
  } catch {
    // Best-effort.
  }
}

function safeRead(storageKey: string): string | null {
  try {
    return localStorage.getItem(storageKey)
  } catch {
    return null
  }
}

// Snapshots are cached by raw string so useSyncExternalStore sees a stable value
// between writes. A write's value is cached even when storage rejects it, so the
// UI still follows the user's choice in private mode or over quota.
const snapshots = new Map<string, { raw: string | null; value: FacetSelection }>()
const listeners = new Map<string, Set<() => void>>()
const EMPTY: FacetSelection = {}

function snapshot(storageKey: string, knownKeys: readonly string[]): FacetSelection {
  const cacheKey = `${storageKey}\n${knownKeys.join(",")}`
  const raw = safeRead(storageKey)
  const cached = snapshots.get(cacheKey)
  if (cached && cached.raw === raw) return cached.value
  const value = parseFacetSelection(raw, knownKeys)
  snapshots.set(cacheKey, { raw, value })
  return value
}

function notify(storageKey: string): void {
  for (const listener of listeners.get(storageKey) ?? []) listener()
}

function subscribe(storageKey: string, listener: () => void): () => void {
  let set = listeners.get(storageKey)
  if (!set) listeners.set(storageKey, (set = new Set()))
  set.add(listener)
  const onStorage = (e: StorageEvent) => {
    if (e.key === storageKey || e.key === null) listener()
  }
  window.addEventListener("storage", onStorage)
  return () => {
    set.delete(listener)
    window.removeEventListener("storage", onStorage)
  }
}

/**
 * A facet selection persisted under `storageKey`, kept in sync across every picker
 * using the key (and other tabs), so one picker's write never clobbers another's.
 * A null key keeps it in memory only (e.g. before the user id is known).
 */
export function usePersistentFacetSelection(
  storageKey: string | null,
  knownKeys: readonly string[],
): [FacetSelection, (next: FacetSelection) => void] {
  const [memory, setMemory] = useState<FacetSelection>(EMPTY)
  const subscribeToKey = useCallback(
    (listener: () => void) => (storageKey ? subscribe(storageKey, listener) : () => {}),
    [storageKey],
  )
  const stored = useSyncExternalStore(subscribeToKey, () =>
    storageKey ? snapshot(storageKey, knownKeys) : EMPTY,
  )

  const setSelection = useCallback(
    (next: FacetSelection) => {
      if (!storageKey) {
        setMemory(next)
        return
      }
      writeFacetSelection(storageKey, next)
      const raw = safeRead(storageKey)
      for (const cacheKey of [...snapshots.keys()]) {
        if (cacheKey.startsWith(`${storageKey}\n`)) snapshots.delete(cacheKey)
      }
      snapshots.set(`${storageKey}\n${knownKeys.join(",")}`, { raw, value: next })
      notify(storageKey)
    },
    [storageKey, knownKeys],
  )

  return [storageKey ? stored : memory, setSelection]
}
