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
