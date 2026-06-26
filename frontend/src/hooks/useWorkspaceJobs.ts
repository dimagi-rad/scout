import { useCallback, useEffect, useRef, useState } from "react"
import { jobsApi, type ActiveJob, type RecentTermination } from "@/api/jobs"

const POLL_INTERVAL_MS = 3000

interface State {
  jobs: ActiveJob[]
  recentTerminations: RecentTermination[]
  lastError: string | null
}

export interface UseWorkspaceJobs {
  jobs: ActiveJob[]
  jobsByThreadId: Record<string, ActiveJob>
  /** Thread IDs whose job just transitioned to a terminal state on the most
   *  recent poll (gone from the active list). Consumers should refetch
   *  thread messages for these IDs. Resets to [] on the next poll cycle. */
  recentlyCompletedThreadIds: string[]
  /** ThreadJobs that terminated within the server's recent-termination
   *  window (default 30 minutes). Used to render an inline failure card on
   *  the run_materialization tool-call once the spinner clears. */
  recentTerminations: RecentTermination[]
  /** Lookup by tool_call_id so ChatMessage can find the termination for a
   *  specific run_materialization card in O(1). Failures with no
   *  tool_call_id (e.g. retry jobs not bound to a card) are excluded. */
  recentTerminationsByToolCallId: Record<string, RecentTermination>
  refresh: () => Promise<void>
  /** Force an immediate poll without waiting for the next tick (called when
   *  the user just fired a chat action that may have started a job). */
  notifyJobLikelyStarted: () => void
}

/**
 * Polling implementation hook. Do NOT call this directly from components —
 * use `useWorkspaceJobs` from `@/contexts/WorkspaceJobsContext` instead so the
 * polling loop has a single owner. The provider is the only legitimate caller.
 */
export function useWorkspaceJobsImpl(workspaceId: string | null): UseWorkspaceJobs {
  const [state, setState] = useState<State>({
    jobs: [],
    recentTerminations: [],
    lastError: null,
  })
  const [recentlyCompletedThreadIds, setRecentlyCompletedThreadIds] = useState<string[]>([])
  const prevThreadIdsRef = useRef<Set<string>>(new Set())

  const fetchOnce = useCallback(async () => {
    if (!workspaceId) return
    try {
      const data = await jobsApi.active(workspaceId)
      const currentThreadIds = new Set(data.jobs.map((j) => j.thread_id))
      const justCompleted: string[] = []
      for (const prev of prevThreadIdsRef.current) {
        if (!currentThreadIds.has(prev)) {
          justCompleted.push(prev)
        }
      }
      prevThreadIdsRef.current = currentThreadIds
      setState({
        jobs: data.jobs,
        recentTerminations: data.recent_terminations ?? [],
        lastError: null,
      })
      if (justCompleted.length > 0) {
        setRecentlyCompletedThreadIds(justCompleted)
      } else {
        // Functional updater so React skips the re-render when the value
        // is already empty (every clean poll would otherwise churn the
        // ChatPanel reload effect).
        setRecentlyCompletedThreadIds((prev) => (prev.length === 0 ? prev : []))
      }
    } catch (e) {
      setState((s) => ({ ...s, lastError: String(e) }))
    }
  }, [workspaceId])

  useEffect(() => {
    if (!workspaceId) return
    // Reset cross-workspace state: prevThreadIdsRef would otherwise carry
    // thread ids from the previous workspace and the first poll in the new
    // workspace would falsely report them as "just completed". Clear
    // recentlyCompletedThreadIds for the same reason.
    prevThreadIdsRef.current = new Set()
    setRecentlyCompletedThreadIds((prev) => (prev.length === 0 ? prev : []))
    let cancelled = false
    let interval: ReturnType<typeof setInterval> | null = null

    // Gate polling on tab visibility (arch #254, 05#6). A hidden tab doesn't
    // need live job status, and each poll triggers an API-side janitor
    // reconciliation; pausing while hidden removes that idle load and resumes
    // (with an immediate catch-up fetch) when the tab is shown again.
    const startPolling = () => {
      if (interval !== null) return
      interval = setInterval(() => {
        if (!cancelled) void fetchOnce()
      }, POLL_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (interval !== null) {
        clearInterval(interval)
        interval = null
      }
    }
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        if (!cancelled) void fetchOnce() // immediate catch-up
        startPolling()
      } else {
        stopPolling()
      }
    }

    document.addEventListener("visibilitychange", handleVisibility)
    // Fire immediately on mount so the UI populates without waiting one tick.
    void fetchOnce()
    if (document.visibilityState === "visible") startPolling()

    return () => {
      cancelled = true
      stopPolling()
      document.removeEventListener("visibilitychange", handleVisibility)
    }
  }, [workspaceId, fetchOnce])

  const jobsByThreadId = state.jobs.reduce<Record<string, ActiveJob>>((acc, j) => {
    acc[j.thread_id] = j
    return acc
  }, {})

  const recentTerminationsByToolCallId = state.recentTerminations.reduce<
    Record<string, RecentTermination>
  >((acc, t) => {
    if (t.tool_call_id && !acc[t.tool_call_id]) acc[t.tool_call_id] = t
    return acc
  }, {})

  return {
    jobs: state.jobs,
    jobsByThreadId,
    recentlyCompletedThreadIds,
    recentTerminations: state.recentTerminations,
    recentTerminationsByToolCallId,
    refresh: fetchOnce,
    notifyJobLikelyStarted: fetchOnce,
  }
}
