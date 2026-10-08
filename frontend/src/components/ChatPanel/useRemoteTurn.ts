import { useEffect, useRef } from "react"

import { ApiError, api } from "@/api/client"

export const REMOTE_TURN_POLL_MS = 3000
// A long turn (or a dead holder's lease, which lapses after 90s) is checked less often.
export const REMOTE_TURN_MAX_POLL_MS = 15_000
const BACKOFF = 1.5
// Past this many failed polls in a row (access revoked, an outage), stop waiting and
// let the reload decide: it shows the turn running again (and the wait restarts), or
// fails and offers its retry. A 403 or outage fails the reload too.
export const REMOTE_TURN_MAX_FAILURES = 5

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
    let failures = 0
    let attempt: AbortController | null = null
    let inFlight = false

    const finish = () => {
      cancelled = true
      onDoneRef.current()
    }

    async function poll() {
      if (typeof document !== "undefined" && document.hidden) {
        // Back on the tab, the next check comes soon: the backoff was never earned.
        delay = REMOTE_TURN_POLL_MS
        timer = setTimeout(poll, REMOTE_TURN_MAX_POLL_MS)
        return
      }
      // A hung request would stop the chain, as the next poll is scheduled after it.
      const controller = new AbortController()
      attempt = controller
      inFlight = true
      const timeout = setTimeout(() => controller.abort(), REMOTE_TURN_MAX_POLL_MS)
      try {
        const response = await api.get<{ turn_running?: boolean }>(
          `/api/workspaces/${workspaceId}/threads/${threadId}/`,
          controller.signal,
        )
        if (cancelled) return
        if (!response.turn_running) {
          finish()
          return
        }
        failures = 0
      } catch (error) {
        if (cancelled) return
        failures += 1
        // Gone, or failing for good: the reload says what became of it.
        if (
          (error instanceof ApiError && error.status === 404)
          || failures >= REMOTE_TURN_MAX_FAILURES
        ) {
          finish()
          return
        }
      } finally {
        inFlight = false
        clearTimeout(timeout)
      }
      delay = Math.min(delay * BACKOFF, REMOTE_TURN_MAX_POLL_MS)
      timer = setTimeout(poll, delay)
    }

    // Back on the tab, check now rather than at the hidden-tab interval.
    const pollOnReturn = () => {
      if (document.hidden || inFlight || cancelled) return
      if (timer) clearTimeout(timer)
      void poll()
    }
    timer = setTimeout(poll, delay)
    document.addEventListener("visibilitychange", pollOnReturn)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
      attempt?.abort()
      document.removeEventListener("visibilitychange", pollOnReturn)
    }
  }, [active, workspaceId, threadId])
}
