import { useCallback, useEffect, useRef, useState } from "react"
import { jobsApi, type ActiveJob } from "@/api/jobs"

const POLL_INTERVAL_MS = 3000

interface State {
  jobs: ActiveJob[]
  lastError: string | null
}

export interface UseWorkspaceJobs {
  jobs: ActiveJob[]
  jobsByThreadId: Record<string, ActiveJob>
  /** Thread IDs whose job just transitioned to a terminal state on the most
   *  recent poll (gone from the active list). Consumers should refetch
   *  thread messages for these IDs. Resets to [] on the next poll cycle. */
  recentlyCompletedThreadIds: string[]
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
  const [state, setState] = useState<State>({ jobs: [], lastError: null })
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
      setState({ jobs: data.jobs, lastError: null })
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
    const interval = setInterval(() => {
      if (!cancelled) void fetchOnce()
    }, POLL_INTERVAL_MS)
    // Fire immediately on mount so the UI populates without waiting one tick.
    void fetchOnce()
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [workspaceId, fetchOnce])

  const jobsByThreadId = state.jobs.reduce<Record<string, ActiveJob>>((acc, j) => {
    acc[j.thread_id] = j
    return acc
  }, {})

  return {
    jobs: state.jobs,
    jobsByThreadId,
    recentlyCompletedThreadIds,
    refresh: fetchOnce,
    notifyJobLikelyStarted: fetchOnce,
  }
}
