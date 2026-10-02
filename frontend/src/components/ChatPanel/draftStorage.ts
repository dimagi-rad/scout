/** localStorage helpers for per-thread composer drafts. */

const DRAFT_PREFIX = "scout:draft:"
export const DRAFT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
export const DRAFT_MAX_ENTRIES = 50

interface StoredDraft {
  text: string
  updatedAt: number
}

function draftKey(workspaceId: string, threadId: string): string {
  return `${DRAFT_PREFIX}${workspaceId}:${threadId}`
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

/** An empty draft removes the entry rather than storing "". */
export function writeDraft(workspaceId: string, threadId: string, text: string): void {
  try {
    const key = draftKey(workspaceId, threadId)
    if (!text) {
      localStorage.removeItem(key)
      return
    }
    localStorage.setItem(key, JSON.stringify({ text, updatedAt: Date.now() }))
  } catch {
    // Storage may be unavailable (private mode, quota). Drafts are best-effort.
  }
}

/** Drop drafts older than 30 days, then the oldest beyond 50 entries. */
export function pruneDrafts(now: number = Date.now()): void {
  try {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(DRAFT_PREFIX)) keys.push(key)
    }
    const live: { key: string; updatedAt: number }[] = []
    for (const key of keys) {
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
