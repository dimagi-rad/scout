import { useCallback, useEffect, useRef, useState } from "react"

import { actionFailure, type ActionFailure } from "@/hooks/useWorkspaceRole"

export type ActionState = "idle" | "pending" | "error"

const FAILURE_VISIBLE_MS = 6000

/**
 * State for a button that fires a write and may be refused. A retryable
 * failure shows its message briefly, then re-arms. A final denial keeps the
 * button disabled with its message until the window regains focus: its copy
 * names a fix made elsewhere (Connected Accounts, usually another tab), and
 * without a re-arm the button would stay dead after the fix.
 */
export function useRetryableAction(fallback: string, canWrite: boolean) {
  const [state, setState] = useState<ActionState>("idle")
  const [failure, setFailure] = useState<ActionFailure | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const reset = useCallback(() => {
    clearTimeout(timer.current)
    setState("idle")
    setFailure(null)
  }, [])

  useEffect(() => () => clearTimeout(timer.current), [])

  useEffect(() => {
    if (!failure || failure.retryable) return
    window.addEventListener("focus", reset)
    return () => window.removeEventListener("focus", reset)
  }, [failure, reset])

  /** Runs `action`; resolves true on success and leaves state "pending". */
  const run = useCallback(
    async (action: () => Promise<unknown>): Promise<boolean> => {
      clearTimeout(timer.current)
      setState("pending")
      setFailure(null)
      try {
        await action()
        return true
      } catch (error) {
        const next = actionFailure(error, fallback, canWrite)
        setFailure(next)
        setState("error")
        if (next.retryable) timer.current = setTimeout(reset, FAILURE_VISIBLE_MS)
        return false
      }
    },
    [fallback, canWrite, reset],
  )

  const settle = useCallback((ms: number) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setState("idle"), ms)
  }, [])

  return {
    state,
    failure,
    /** True while a request is in flight or a final denial stands. */
    blocked: state === "pending" || (failure !== null && !failure.retryable),
    run,
    settle,
  }
}
