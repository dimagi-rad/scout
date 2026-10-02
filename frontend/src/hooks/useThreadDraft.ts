import { useCallback, useEffect, useRef, useState } from "react"

import { pruneDrafts, readDraft, writeDraft } from "@/components/ChatPanel/draftStorage"

export const DRAFT_DEBOUNCE_MS = 300

interface DraftState {
  scope: string
  value: string
}

function scopeOf(workspaceId: string | null, threadId: string | null): string {
  return workspaceId && threadId ? `${workspaceId}\u0000${threadId}` : ""
}

function load(workspaceId: string | null, threadId: string | null): string {
  return workspaceId && threadId ? readDraft(workspaceId, threadId) : ""
}

/**
 * Composer text persisted per (workspace, thread). Switching threads swaps the
 * text; edits are written to localStorage after a short debounce, and an empty
 * value (e.g. after a send) is removed immediately.
 */
export function useThreadDraft(
  workspaceId: string | null,
  threadId: string | null,
): [string, (value: string) => void] {
  const scope = scopeOf(workspaceId, threadId)
  const [state, setState] = useState<DraftState>(() => ({
    scope,
    value: load(workspaceId, threadId),
  }))
  const pendingRef = useRef<{ workspaceId: string; threadId: string; value: string } | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Adjust state during render so the new thread never paints the old thread's text.
  if (state.scope !== scope) {
    setState({ scope, value: load(workspaceId, threadId) })
  }

  const flush = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
    const pending = pendingRef.current
    pendingRef.current = null
    if (pending) writeDraft(pending.workspaceId, pending.threadId, pending.value)
  }, [])

  useEffect(() => {
    pruneDrafts()
  }, [])

  // Persist the outgoing thread's pending edit before the scope changes or we unmount.
  useEffect(() => flush, [scope, flush])

  const setValue = useCallback(
    (value: string) => {
      setState({ scope, value })
      if (!workspaceId || !threadId) return
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = null
      if (value === "") {
        pendingRef.current = null
        writeDraft(workspaceId, threadId, "")
        return
      }
      pendingRef.current = { workspaceId, threadId, value }
      timerRef.current = setTimeout(flush, DRAFT_DEBOUNCE_MS)
    },
    [scope, workspaceId, threadId, flush],
  )

  return [state.scope === scope ? state.value : load(workspaceId, threadId), setValue]
}
