import { useCallback, useEffect, useRef, useState } from "react"

import {
  draftGeneration,
  pruneDrafts,
  readDraft,
  writeDraft,
  type DraftScope,
} from "@/components/ChatPanel/draftStorage"

export const DRAFT_DEBOUNCE_MS = 300

interface DraftState {
  scope: string
  value: string
}

function draftScope(
  userId: string | null,
  workspaceId: string | null,
  threadId: string | null,
): DraftScope | null {
  return userId && workspaceId && threadId ? { userId, workspaceId, threadId } : null
}

function scopeKey(target: DraftScope | null): string {
  return target ? `${target.userId}\u0000${target.workspaceId}\u0000${target.threadId}` : ""
}

/**
 * Composer text persisted per (user, workspace, thread). Switching threads swaps the
 * text; edits are written to localStorage after a short debounce, and an empty
 * value (e.g. after a send) is removed immediately.
 */
export function useThreadDraft(
  userId: string | null,
  workspaceId: string | null,
  threadId: string | null,
): [string, (value: string) => void] {
  const target = draftScope(userId, workspaceId, threadId)
  const scope = scopeKey(target)
  const [state, setState] = useState<DraftState>(() => ({
    scope,
    value: target ? readDraft(target) : "",
  }))
  const pendingRef = useRef<{ target: DraftScope; value: string; generation: number } | null>(
    null,
  )
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Adjust state during render so the new thread never paints the old thread's text.
  if (state.scope !== scope) {
    setState({ scope, value: target ? readDraft(target) : "" })
  }

  const flush = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
    const pending = pendingRef.current
    pendingRef.current = null
    if (pending) writeDraft(pending.target, pending.value, pending.generation)
  }, [])

  useEffect(() => {
    pruneDrafts()
  }, [])

  // Persist the outgoing thread's pending edit before the scope changes or we unmount.
  useEffect(() => flush, [scope, flush])

  // Unmount cleanup does not run on reload/tab close, which would lose the last debounce window.
  useEffect(() => {
    window.addEventListener("pagehide", flush)
    return () => window.removeEventListener("pagehide", flush)
  }, [flush])

  const setValue = useCallback(
    (value: string) => {
      setState({ scope, value })
      const current = draftScope(userId, workspaceId, threadId)
      if (!current) return
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = null
      if (value === "") {
        pendingRef.current = null
        writeDraft(current, "")
        return
      }
      pendingRef.current = { target: current, value, generation: draftGeneration() }
      timerRef.current = setTimeout(flush, DRAFT_DEBOUNCE_MS)
    },
    [scope, userId, workspaceId, threadId, flush],
  )

  return [state.value, setValue]
}
