// Tracks recently-used workspaces client-side. The backend has no "recent"
// concept, so we persist an ordered, deduped list of workspace ids in
// localStorage (newest first). Resilient to storage being unavailable
// (private mode, quota, SSR) — failures degrade to "no recents".

const STORAGE_KEY = "scout.recentWorkspaces"
const MAX_RECENTS = 12

function readRaw(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((id): id is string => typeof id === "string")
  } catch {
    return []
  }
}

/** Workspace ids most-recently used first. */
export function getRecentWorkspaceIds(): string[] {
  return readRaw()
}

/** Record that a workspace was activated, moving it to the front. */
export function recordWorkspaceUse(id: string): void {
  if (!id) return
  try {
    const next = [id, ...readRaw().filter((existing) => existing !== id)].slice(0, MAX_RECENTS)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  } catch {
    // Ignore — recents are a best-effort convenience.
  }
}
