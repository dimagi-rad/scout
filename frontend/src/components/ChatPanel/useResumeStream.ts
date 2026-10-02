import { useCallback, useEffect, useRef, useState } from "react"

import { api } from "@/api/client"

export const RESUME_STREAM_POLL_MS = 500
// While nothing new arrives (a tool running, a queued resume), poll less often.
export const RESUME_STREAM_IDLE_POLL_MS = 3000

interface StreamChunk {
  id: number
  run: string
  text: string
  done: boolean
}

interface StreamState {
  scope: string
  run: string | null
  text: string
}

/**
 * The answer a background resume of this chat is writing, tailed while
 * ``active`` (the resume is running). The text stays until ``reset``, which the
 * chat calls once its reloaded messages carry the final answer, so the answer
 * never blinks out between the two.
 */
export function useResumeStream(
  workspaceId: string | null,
  threadId: string,
  active: boolean,
): { text: string; reset: () => void } {
  const scope = `${workspaceId}\u0000${threadId}`
  const [state, setState] = useState<StreamState>({ scope, run: null, text: "" })
  if (state.scope !== scope) setState({ scope, run: null, text: "" })
  // Where this chat has read up to; per chat, so a switch starts over.
  const cursorRef = useRef<{ scope: string; after: number; caughtUp: boolean }>({
    scope,
    after: 0,
    caughtUp: false,
  })

  useEffect(() => {
    if (!active || !workspaceId) return
    if (cursorRef.current.scope !== scope) cursorRef.current = { scope, after: 0, caughtUp: false }
    // Each resume is read afresh: a run that ended while nothing was tailing it
    // (its last rows never read) is an earlier answer, not this one.
    cursorRef.current.caughtUp = false
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let delay = RESUME_STREAM_POLL_MS

    async function poll() {
      const cursor = cursorRef.current
      if (typeof document !== "undefined" && document.hidden) {
        timer = setTimeout(poll, RESUME_STREAM_IDLE_POLL_MS)
        return
      }
      try {
        const { chunks } = await api.get<{ chunks: StreamChunk[] }>(
          `/api/workspaces/${workspaceId}/threads/${threadId}/resume-stream/?after=${cursor.after}`,
        )
        if (cancelled || cursorRef.current !== cursor) return
        if (chunks.length) cursor.after = chunks[chunks.length - 1].id
        // On the first read, a run already done is an earlier answer the
        // conversation already shows; only one still being written is news.
        const finished = cursor.caughtUp
          ? new Set<string>()
          : new Set(chunks.filter((chunk) => chunk.done).map((chunk) => chunk.run))
        cursor.caughtUp = true
        const fresh = chunks.filter((chunk) => !finished.has(chunk.run))
        delay = fresh.length
          ? RESUME_STREAM_POLL_MS
          : Math.min(delay * 2, RESUME_STREAM_IDLE_POLL_MS)
        if (fresh.length) {
          setState((prev) => {
            let { run, text } = prev
            for (const chunk of fresh) {
              if (chunk.run !== run) {
                run = chunk.run
                text = ""
              }
              text += chunk.text
            }
            return { ...prev, run, text }
          })
        }
      } catch {
        // A missed poll is caught up by the next; the answer also lands on reload.
      }
      if (!cancelled) timer = setTimeout(poll, delay)
    }

    void poll()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [active, workspaceId, threadId, scope])

  const reset = useCallback(() => {
    setState((prev) => (prev.text === "" && prev.run === null ? prev : { ...prev, run: null, text: "" }))
  }, [])

  return { text: state.text, reset }
}
