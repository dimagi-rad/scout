/** localStorage helpers for remembering the last-viewed thread per workspace. */

function threadStorageKey(workspaceId: string): string {
  return `scout:thread:${workspaceId}`
}

export function readSavedThreadId(workspaceId: string): string | null {
  try {
    return localStorage.getItem(threadStorageKey(workspaceId))
  } catch {
    return null
  }
}

export function writeSavedThreadId(workspaceId: string, threadId: string): void {
  try {
    localStorage.setItem(threadStorageKey(workspaceId), threadId)
  } catch {
    // Storage may be unavailable (private mode, quota). Persistence is best-effort.
  }
}

/**
 * Remove the saved thread for a workspace, but only if it still matches
 * `threadId`. The match guard avoids clobbering a newer saved thread when an
 * older (stale) thread's load fails.
 */
export function clearSavedThreadId(workspaceId: string, threadId?: string): void {
  try {
    if (threadId && readSavedThreadId(workspaceId) !== threadId) return
    localStorage.removeItem(threadStorageKey(workspaceId))
  } catch {
    // Storage may be unavailable; clearing is best-effort.
  }
}
