import { useEffect, useRef } from "react"

import { ApiError, api } from "@/api/client"

export const REMOTE_TURN_POLL_MS = 3000
// A long turn (or a dead holder's lease, which lapses after 90s) is checked less often.
export const REMOTE_TURN_MAX_POLL_MS = 15_000
const BACKOFF = 1.5

/**
 * While ``active`` (the server reports this thread's turn running and this tab is not
 * running it), poll the thread until the turn is over, then call ``onDone`` once.
 * Stops on unmount and when the thread changes. It reads the thread's row, not its
 * messages: the reload on done fetches those once, instead of the checkpoint each poll.
 */
export function useRemoteTurnPoll(
  workspaceId: string | null,
  threadId: string,
  active: boolean,
  onDone: () => void,
): void {
  const onDoneRef = useRef(onDone)
  useEffect(() => {
    onDoneRef.current = onDone
  })

  useEffect(() => {
    if (!active || !workspaceId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let delay = REMOTE_TURN_POLL_MS

    async function poll() {
      if (typeof document !== "undefined" && document.hidden) {
        timer = setTimeout(poll, REMOTE_TURN_MAX_POLL_MS)
        return
      }
      try {
        const response = await api.get<{ turn_running?: boolean }>(
          `/api/workspaces/${workspaceId}/threads/${threadId}/`,
        )
        if (cancelled) return
        if (!response.turn_running) {
          onDoneRef.current()
          return
        }
      } catch (error) {
        if (cancelled) return
        // Gone: the reload says what became of it.
        if (error instanceof ApiError && error.status === 404) {
          onDoneRef.current()
          return
        }
      }
      delay = Math.min(delay * BACKOFF, REMOTE_TURN_MAX_POLL_MS)
      timer = setTimeout(poll, delay)
    }

    timer = setTimeout(poll, delay)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [active, workspaceId, threadId])
}
