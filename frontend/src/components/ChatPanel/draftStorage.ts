/** localStorage helpers for per-thread composer drafts. */

const DRAFT_PREFIX = "scout:draft:"
export const DRAFT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
export const DRAFT_MAX_ENTRIES = 50
export const DRAFT_MAX_CHARS = 100_000

interface StoredDraft {
  text: string
  updatedAt: number
}

// Bumped by clearAllDrafts so writes queued before a clear (debounce timer,
// unmount/pagehide flush) cannot resurrect drafts after logout.
let generation = 0

export function draftGeneration(): number {
  return generation
}

function draftKey(workspaceId: string, threadId: string): string {
  return `${DRAFT_PREFIX}${workspaceId}:${threadId}`
}

function draftKeys(): string[] {
  const keys: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key?.startsWith(DRAFT_PREFIX)) keys.push(key)
  }
  return keys
}

function parseDraft(raw: string | null): StoredDraft | null {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    if (typeof parsed?.text === "string" && typeof parsed?.updatedAt === "number") {
      return parsed as StoredDraft
    }
  } catch {
    // Malformed entry; treated as absent (and pruned).
  }
  return null
}

export function readDraft(workspaceId: string, threadId: string): string {
  try {
    return parseDraft(localStorage.getItem(draftKey(workspaceId, threadId)))?.text ?? ""
  } catch {
    return ""
  }
}

/**
 * An empty draft removes the entry rather than storing "". Oversized drafts are
 * skipped so a huge paste cannot exhaust the origin's quota. A write tagged with
 * a generation older than the last clearAllDrafts is dropped.
 */
export function writeDraft(
  workspaceId: string,
  threadId: string,
  text: string,
  writtenGeneration: number = generation,
): void {
  if (writtenGeneration !== generation) return
  try {
    const key = draftKey(workspaceId, threadId)
    if (!text) {
      localStorage.removeItem(key)
      return
    }
    if (text.length > DRAFT_MAX_CHARS) return
    const payload = JSON.stringify({ text, updatedAt: Date.now() })
    try {
      localStorage.setItem(key, payload)
    } catch {
      // Likely quota: drop stale drafts and retry once.
      pruneDrafts()
      localStorage.setItem(key, payload)
    }
  } catch {
    // Storage may be unavailable (private mode, quota). Drafts are best-effort.
  }
}

/** Drafts hold query text, so they must not outlive the session that wrote them. */
export function clearAllDrafts(): void {
  generation++
  try {
    for (const key of draftKeys()) localStorage.removeItem(key)
  } catch {
    // Best-effort.
  }
}

/** Drop drafts older than 30 days, then the oldest beyond 50 entries. */
export function pruneDrafts(now: number = Date.now()): void {
  try {
    const live: { key: string; updatedAt: number }[] = []
    for (const key of draftKeys()) {
      const draft = parseDraft(localStorage.getItem(key))
      if (!draft || now - draft.updatedAt > DRAFT_MAX_AGE_MS) {
        localStorage.removeItem(key)
      } else {
        live.push({ key, updatedAt: draft.updatedAt })
      }
    }
    live.sort((a, b) => b.updatedAt - a.updatedAt)
    for (const { key } of live.slice(DRAFT_MAX_ENTRIES)) localStorage.removeItem(key)
  } catch {
    // Pruning is best-effort.
  }
}
